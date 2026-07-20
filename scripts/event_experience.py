"""Non-canonical event-level reader-experience control plane.

This module deliberately owns no continuity facts.  It stores event seeds,
reader-experience arcs, immutable contract payloads, reviews, and one bounded
clarification question in control tables that are excluded from canon replay.

The public service uses a database-wide control revision for compare-and-swap
(CAS).  Successful semantic mutations advance that revision exactly once;
idempotent retries return the original response.  A locked contract payload is
never edited in place: changes create a new contract that explicitly
supersedes the old row.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


EVENT_EXPERIENCE_SCHEMA_VERSION = "plot-rag-event-experience/v1"
EVENT_EXPERIENCE_DATABASE_SCHEMA_VERSION = 1
EVENT_EXPERIENCE_HASH_PROTOCOL = (
    "plot-rag-event-experience-hash/codepoint-json-v1"
)
_HASH_PROTOCOL_META_KEY = "hash_protocol"
_HASH_PROTOCOL_DOMAIN = (
    EVENT_EXPERIENCE_HASH_PROTOCOL.encode("ascii") + b"\0"
)

SEED_STATUSES = (
    "seeded",
    "experience_locked",
    "expanded",
    "generated",
    "retired",
)
ARC_STATUSES = ("proposed", "locked", "retired")
CONTRACT_STATUSES = ("proposed", "locked", "retired")
REVIEW_STATUSES = ("recorded", "superseded", "retired")
QUESTION_STATUSES = (
    "AWAITING_ANSWER",
    "AWAITING_EVENT_EXPERIENCE",
    "ANSWERED",
    "RETIRED",
)

CONTROL_TABLES = frozenset(
    {
        "event_experience_meta",
        "event_seeds",
        "event_experience_arcs",
        "event_experience_contracts",
        "event_experience_reviews",
        "event_experience_observed_reviews",
        "event_experience_questions",
        "event_experience_idempotency",
    }
)

_CONTRACT_HASH_EXCLUDED = frozenset(
    {
        "contract_hash",
        "status",
        "locked_at",
        "retired_at",
        "retired_reason",
        "created_at",
        "updated_at",
    }
)
_LOCKED_PROVENANCE_FIELDS = frozenset(
    {
        "entry_reader_state",
        "target_reader_state",
        "primary_emotion",
        "emotional_turn",
        "intensity",
        "emotion_curve",
        "mechanisms",
        "reader_knowledge_position",
        "viewpoint_character_state",
        "payoff_or_reveal",
        "aftertaste",
        "anti_experiences",
        "success_signals",
    }
)
_REPEAT_ANSWERS = frozenset(
    {
        "",
        "继续",
        "继续吧",
        "继续推进",
        "下一步",
        "开始",
        "开始吧",
        "接着来",
        "照此执行",
        "按计划推进",
    }
)
_RECOMMENDED_ANSWERS = frozenset(
    {
        "按推荐答案",
        "采用推荐答案",
        "用推荐答案",
        "你来定",
        "模型决定",
        "按你的推荐",
    }
)
_CANCEL_ANSWERS = frozenset(
    {
        "cancel",
        "取消",
        "取消本轮",
        "取消事件体验",
        "放弃本轮事件",
    }
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS event_experience_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_seeds(
    event_seed_id TEXT NOT NULL,
    event_seed_revision INTEGER NOT NULL,
    parent_chain_id TEXT NOT NULL,
    dependency_order INTEGER NOT NULL,
    seed_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    experience_contract_id TEXT,
    experience_contract_hash TEXT,
    supersedes_seed_revision INTEGER,
    retired_at TEXT,
    retired_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(event_seed_id, event_seed_revision)
);

CREATE INDEX IF NOT EXISTS idx_event_seeds_chain
ON event_seeds(parent_chain_id, dependency_order, event_seed_id);

CREATE TABLE IF NOT EXISTS event_experience_arcs(
    arc_id TEXT NOT NULL,
    arc_revision INTEGER NOT NULL,
    parent_chain_id TEXT NOT NULL,
    arc_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    supersedes_arc_revision INTEGER,
    locked_at TEXT,
    retired_at TEXT,
    retired_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(arc_id, arc_revision)
);

CREATE INDEX IF NOT EXISTS idx_event_experience_arcs_chain
ON event_experience_arcs(parent_chain_id, arc_revision);

CREATE TABLE IF NOT EXISTS event_experience_contracts(
    contract_id TEXT PRIMARY KEY,
    contract_revision INTEGER NOT NULL,
    event_seed_id TEXT NOT NULL,
    event_seed_revision INTEGER NOT NULL,
    parent_chain_id TEXT NOT NULL,
    contract_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    supersedes_contract_id TEXT,
    locked_at TEXT,
    retired_at TEXT,
    retired_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(event_seed_id, event_seed_revision)
        REFERENCES event_seeds(event_seed_id, event_seed_revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_event_experience_contract_active_seed
ON event_experience_contracts(event_seed_id, event_seed_revision)
WHERE status IN ('proposed', 'locked');

CREATE INDEX IF NOT EXISTS idx_event_experience_contract_chain
ON event_experience_contracts(parent_chain_id, event_seed_id, contract_revision);

CREATE TABLE IF NOT EXISTS event_experience_reviews(
    review_id TEXT PRIMARY KEY,
    review_revision INTEGER NOT NULL,
    proposal_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    assistant_sha256 TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    contract_hash TEXT NOT NULL,
    review_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    supersedes_review_id TEXT,
    retired_at TEXT,
    retired_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(contract_id) REFERENCES event_experience_contracts(contract_id)
);

CREATE INDEX IF NOT EXISTS idx_event_experience_reviews_contract
ON event_experience_reviews(contract_id, review_revision);

CREATE TABLE IF NOT EXISTS event_experience_observed_reviews(
    review_id TEXT PRIMARY KEY,
    review_revision INTEGER NOT NULL,
    artifact_id TEXT NOT NULL,
    artifact_revision INTEGER NOT NULL,
    branch_id TEXT NOT NULL,
    source_commit_id TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    assistant_sha256 TEXT NOT NULL,
    review_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    supersedes_review_id TEXT,
    retired_at TEXT,
    retired_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_experience_observed_reviews_artifact
ON event_experience_observed_reviews(
    artifact_id, branch_id, artifact_revision, review_revision
);

CREATE TABLE IF NOT EXISTS event_experience_questions(
    event_seed_manifest_hash TEXT PRIMARY KEY,
    question_hash TEXT NOT NULL,
    question_json TEXT NOT NULL,
    status TEXT NOT NULL,
    invalid_attempts INTEGER NOT NULL DEFAULT 0,
    selected_option_id TEXT,
    selected_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_experience_idempotency(
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(operation, idempotency_key)
);
"""


class EventExperienceError(RuntimeError):
    """Stable control-plane failure with machine-readable code and details."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        self.code = str(code)
        self.details = dict(details)
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True)
class EventSeed:
    """Minimum non-canonical causal event declaration."""

    parent_chain_id: str
    dependency_order: int
    dramatic_function: str
    causal_role: str
    intended_state_change: str
    event_boundary: str
    event_seed_id: str = ""
    event_seed_revision: int = 1
    narrative_event_id: str = ""
    artifact_id: str = ""
    artifact_revision: int = 0
    branch_id: str = "main"
    chapter_no: int | None = None
    scene_index: int | None = None
    source_outline_commit_id: str = ""
    source_outline_artifact_version_id: str = ""
    source_outline_artifact_id: str = ""
    source_outline_artifact_revision: int = 0
    source_outline_content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EventExperienceArc:
    """Chain-level emotional trajectory; it never replaces event contracts."""

    parent_chain_id: str
    entry_reader_state: str
    target_reader_state: str
    overall_peak: str
    release_rhythm: str
    aftertaste: str
    event_seed_ids: tuple[str, ...]
    arc_id: str = ""
    arc_revision: int = 1
    branch_id: str = "main"
    artifact_id: str = ""
    artifact_revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EventExperienceContract:
    """Frozen event-level reader-experience contract candidate."""

    event_seed_id: str
    event_seed_revision: int
    entry_reader_state: str
    target_reader_state: str
    primary_emotion: str
    ordered_secondary_emotions: tuple[str, ...]
    emotional_turn: str
    intensity: Mapping[str, int]
    emotion_curve: tuple[str, ...]
    mechanisms: tuple[str, ...]
    reader_knowledge_position: str
    viewpoint_character_state: str
    payoff_or_reveal: str
    aftertaste: str
    anti_experiences: tuple[str, ...]
    success_signals: tuple[str, ...]
    derivation: Mapping[str, Any]
    contract_id: str = ""
    contract_revision: int = 1
    narrative_event_id: str = ""
    source_intent_contract_id: str = ""
    source_intent_contract_revision: int = 0
    source_intent_contract_hash: str = ""
    open_loop_links: tuple[str, ...] = ()
    field_provenance: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExperienceReview:
    """Observed, non-canonical comparison of generated text with a contract."""

    proposal_id: str
    receipt_id: str
    assistant_sha256: str
    contract_id: str
    contract_hash: str
    artifact_revision: int
    observed_entry: str
    observed_peak: str
    observed_exit: str
    supporting_quotes: tuple[str, ...]
    supporting_quote_offsets: tuple[tuple[int, int], ...]
    drift: str
    severity: str
    recommendation: str
    review_id: str = ""
    review_revision: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_TIMESTAMP_INVALID",
            "timestamp must be valid ISO-8601",
            value=str(value),
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normal_string(value: str) -> str:
    """Apply the versioned code-point protocol without Unicode tables.

    Unicode normalization data changes between supported CPython releases.
    Authority hashes therefore preserve Unicode scalar values exactly and
    normalize only line endings, whose mapping is explicitly frozen here.
    """

    normalized = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in normalized):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_INVALID_UNICODE_SCALAR",
            "hash payload contains an isolated Unicode surrogate",
        )
    return normalized


def normalize_for_hash(value: Any) -> Any:
    """Return a JSON-safe deterministic value used for all control hashes."""

    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_NONFINITE_NUMBER",
                "hash payload contains a non-finite number",
            )
        return value
    if isinstance(value, str):
        return _normal_string(value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_NONSTRING_KEY",
                    "hash payload object keys must be strings",
                )
            normalized_key = _normal_string(key)
            if normalized_key in normalized:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_NORMALIZED_KEY_COLLISION",
                    "hash payload contains object keys that normalize to the same value",
                    key=normalized_key,
                )
            normalized[normalized_key] = normalize_for_hash(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [normalize_for_hash(item) for item in value]
    raise EventExperienceError(
        "EVENT_EXPERIENCE_UNSUPPORTED_VALUE",
        f"unsupported hash payload type: {type(value).__name__}",
    )


def canonical_json(value: Any) -> str:
    """Serialize using stable code points, key order, and separators."""

    return json.dumps(
        normalize_for_hash(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _storage_json(value: Any) -> str:
    """Serialize validated values without rewriting verbatim evidence strings."""

    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(
    value: Any,
    *,
    exclude_top_level: Sequence[str] = (),
) -> str:
    """Compute a domain-separated SHA-256 under the frozen hash protocol."""

    normalized = normalize_for_hash(value)
    if exclude_top_level:
        if not isinstance(normalized, dict):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_HASH_SHAPE",
                "top-level hash exclusions require an object",
            )
        excluded = set(exclude_top_level)
        normalized = {
            key: item for key, item in normalized.items() if key not in excluded
        }
    payload = canonical_json(normalized).encode("utf-8")
    return hashlib.sha256(_HASH_PROTOCOL_DOMAIN + payload).hexdigest()


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}-{canonical_hash(payload)[:24]}"


def _require_text(value: Any, field: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventExperienceError(
            "EVENT_EXPERIENCE_FIELD_REQUIRED",
            f"{field} must be a non-empty string",
            field=field,
        )
    normalized = _normal_string(value).strip()
    if len(normalized) > maximum:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_FIELD_TOO_LONG",
            f"{field} exceeds {maximum} characters",
            field=field,
            maximum=maximum,
        )
    return normalized


def _optional_text(value: Any, field: str, *, maximum: int = 4096) -> str:
    if value is None or value == "":
        return ""
    return _require_text(value, field, maximum=maximum)


def _require_sha256(value: Any, field: str) -> str:
    digest = _require_text(value, field, maximum=64)
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_SHA256_REQUIRED",
            f"{field} must be a lowercase SHA-256 digest",
            field=field,
        )
    return digest


def _require_int(
    value: Any,
    field: str,
    *,
    minimum: int = 0,
    maximum: int = 2_147_483_647,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_INTEGER_REQUIRED",
            f"{field} must be an integer",
            field=field,
        )
    if value < minimum or value > maximum:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_INTEGER_RANGE",
            f"{field} must be between {minimum} and {maximum}",
            field=field,
            minimum=minimum,
            maximum=maximum,
        )
    return int(value)


def _text_list(
    value: Any,
    field: str,
    *,
    required: bool = False,
    maximum_items: int = 64,
) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_LIST_REQUIRED",
            f"{field} must be an array of strings",
            field=field,
        )
    result = [
        _require_text(item, f"{field}[{index}]", maximum=1024)
        for index, item in enumerate(value)
    ]
    if required and not result:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_FIELD_REQUIRED",
            f"{field} must contain at least one item",
            field=field,
        )
    if len(result) > maximum_items:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_LIST_TOO_LONG",
            f"{field} exceeds {maximum_items} items",
            field=field,
        )
    return result


def _verbatim_text_list(
    value: Any,
    field: str,
    *,
    required: bool = False,
    maximum_items: int = 64,
) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_LIST_REQUIRED",
            f"{field} must be an array of strings",
            field=field,
        )
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_FIELD_REQUIRED",
                f"{field}[{index}] must be a non-empty string",
                field=f"{field}[{index}]",
            )
        if len(item) > 4096:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_FIELD_TOO_LONG",
                f"{field}[{index}] exceeds 4096 characters",
                field=f"{field}[{index}]",
                maximum=4096,
            )
        result.append(item)
    if required and not result:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_FIELD_REQUIRED",
            f"{field} must contain at least one item",
            field=field,
        )
    if len(result) > maximum_items:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_LIST_TOO_LONG",
            f"{field} exceeds {maximum_items} items",
            field=field,
        )
    return result


def _mapping_input(value: Any, type_name: str) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if not isinstance(value, Mapping):
        raise EventExperienceError(
            "EVENT_EXPERIENCE_OBJECT_REQUIRED",
            f"{type_name} must be an object",
        )
    return dict(value)


def _reject_unknown(payload: Mapping[str, Any], allowed: set[str], type_name: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_UNKNOWN_FIELD",
            f"{type_name} contains unsupported fields: {unknown}",
            fields=unknown,
        )


def _validate_supplied_schema(
    payload: Mapping[str, Any],
    type_name: str,
) -> None:
    supplied = payload.get("schema_version")
    if supplied is not None and supplied != EVENT_EXPERIENCE_SCHEMA_VERSION:
        raise EventExperienceError(
            "EVENT_EXPERIENCE_SCHEMA_VERSION",
            f"{type_name} schema_version is not supported",
            supplied=supplied,
            supported=EVENT_EXPERIENCE_SCHEMA_VERSION,
        )


class EventExperienceService:
    """SQLite-backed event-experience control service."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path).expanduser().resolve(strict=False)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap_shared_state_database()
        self._ensure_schema()

    @classmethod
    def for_project(cls, project_root: Path | str) -> "EventExperienceService":
        root = Path(project_root).expanduser().resolve(strict=False)
        return cls(root / ".plot-rag" / "state.sqlite3")

    @staticmethod
    def _is_shared_state_database(path: Path) -> bool:
        """Match the project database using the host filesystem semantics."""

        if os.name == "nt":
            return (
                path.name.casefold() == "state.sqlite3"
                and path.parent.name.casefold() == ".plot-rag"
            )
        return (
            path.name == "state.sqlite3"
            and path.parent.name == ".plot-rag"
        )

    def _bootstrap_shared_state_database(self) -> None:
        """Create the authoritative continuity schema before control tables.

        The project-scoped event-experience service shares
        ``.plot-rag/state.sqlite3`` with continuity.  Creating its control
        tables first would leave an existing user database without the
        required continuity version marker, which the fail-closed continuity
        migrator must reject.  Standalone control-plane databases keep their
        independent lightweight schema.
        """

        if not self._is_shared_state_database(self.database_path):
            return
        if __package__:
            from .continuity.store import ContinuityStore
        else:
            from continuity.store import ContinuityStore

        project_root = self.database_path.parent.parent
        ContinuityStore(
            project_root,
            db_path=self.database_path,
        ).ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _ensure_schema(self) -> None:
        with self._transaction(write=True) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                    """
                )
            }
            owned_without_meta = (tables & CONTROL_TABLES) - {
                "event_experience_meta"
            }
            if owned_without_meta and "event_experience_meta" not in tables:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SCHEMA_MISSING",
                    "control tables exist without event_experience_meta",
                    tables=sorted(owned_without_meta),
                )
            if "event_experience_meta" in tables:
                row = connection.execute(
                    """
                    SELECT value FROM event_experience_meta
                    WHERE key='schema_version'
                    """
                ).fetchone()
                if row is None:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_SCHEMA_MISSING",
                        "schema_version metadata is missing",
                    )
                if str(row[0]) != str(EVENT_EXPERIENCE_DATABASE_SCHEMA_VERSION):
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_SCHEMA_UNSUPPORTED",
                        "stored event-experience schema is not supported",
                        stored=str(row[0]),
                        supported=EVENT_EXPERIENCE_DATABASE_SCHEMA_VERSION,
                    )
            for statement in self._split_sql(_SCHEMA_SQL):
                connection.execute(statement)
            connection.execute(
                """
                INSERT OR IGNORE INTO event_experience_meta(key, value)
                VALUES('schema_version', ?)
                """,
                (str(EVENT_EXPERIENCE_DATABASE_SCHEMA_VERSION),),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO event_experience_meta(key, value)
                VALUES('control_revision', '0')
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO event_experience_meta(key, value)
                SELECT 'binding_revision', value
                FROM event_experience_meta
                WHERE key='control_revision'
                """
            )
            self._ensure_hash_protocol(connection)

    @staticmethod
    def _ensure_hash_protocol(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            """
            SELECT value FROM event_experience_meta
            WHERE key=?
            """,
            (_HASH_PROTOCOL_META_KEY,),
        ).fetchone()
        if row is not None:
            stored = str(row[0])
            if stored != EVENT_EXPERIENCE_HASH_PROTOCOL:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_HASH_PROTOCOL_UNSUPPORTED",
                    "stored event-experience hash protocol is not supported",
                    stored=stored,
                    supported=EVENT_EXPERIENCE_HASH_PROTOCOL,
                )
            return

        row_counts = {
            table: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
            )
            for table in sorted(CONTROL_TABLES - {"event_experience_meta"})
        }
        revision_row = connection.execute(
            """
            SELECT value FROM event_experience_meta
            WHERE key='control_revision'
            """
        ).fetchone()
        try:
            control_revision = int(
                revision_row[0] if revision_row is not None else 0
            )
        except (TypeError, ValueError) as exc:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_REVISION_UNREADABLE",
                "stored control revision is not an integer",
            ) from exc
        if control_revision != 0 or any(row_counts.values()):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_HASH_PROTOCOL_MISSING",
                (
                    "existing control-plane data has no hash protocol; "
                    "automatic rehashing is forbidden because external "
                    "receipt/proposal bindings may already reference it"
                ),
                required=EVENT_EXPERIENCE_HASH_PROTOCOL,
                control_revision=control_revision,
                row_counts=row_counts,
            )
        connection.execute(
            """
            INSERT INTO event_experience_meta(key, value)
            VALUES(?, ?)
            """,
            (
                _HASH_PROTOCOL_META_KEY,
                EVENT_EXPERIENCE_HASH_PROTOCOL,
            ),
        )

    @staticmethod
    def _hash_protocol(connection: sqlite3.Connection) -> str:
        row = connection.execute(
            """
            SELECT value FROM event_experience_meta
            WHERE key=?
            """,
            (_HASH_PROTOCOL_META_KEY,),
        ).fetchone()
        if row is None:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_HASH_PROTOCOL_MISSING",
                "event-experience hash protocol metadata is missing",
                required=EVENT_EXPERIENCE_HASH_PROTOCOL,
            )
        protocol = str(row[0])
        if protocol != EVENT_EXPERIENCE_HASH_PROTOCOL:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_HASH_PROTOCOL_UNSUPPORTED",
                "stored event-experience hash protocol is not supported",
                stored=protocol,
                supported=EVENT_EXPERIENCE_HASH_PROTOCOL,
            )
        return protocol

    @staticmethod
    def _split_sql(script: str) -> Iterator[str]:
        statement = ""
        for line in str(script).splitlines():
            statement += line + "\n"
            if not sqlite3.complete_statement(statement):
                continue
            sql = statement.strip()
            statement = ""
            if sql:
                yield sql
        if statement.strip():
            raise EventExperienceError(
                "EVENT_EXPERIENCE_SCHEMA_INVALID",
                "event-experience schema SQL is incomplete",
            )

    @staticmethod
    def _control_revision(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            """
            SELECT value FROM event_experience_meta
            WHERE key='control_revision'
            """
        ).fetchone()
        if row is None:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_SCHEMA_MISSING",
                "control_revision metadata is missing",
            )
        try:
            revision = int(row[0])
        except (TypeError, ValueError) as exc:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_REVISION_UNREADABLE",
                "stored control revision is not an integer",
            ) from exc
        if revision < 0:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_REVISION_UNREADABLE",
                "stored control revision is negative",
            )
        return revision

    def get_control_revision(self) -> int:
        with self._transaction(write=False) as connection:
            return self._control_revision(connection)

    @staticmethod
    def _binding_revision(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            """
            SELECT value FROM event_experience_meta
            WHERE key='binding_revision'
            """
        ).fetchone()
        if row is None:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_SCHEMA_MISSING",
                "binding_revision metadata is missing",
            )
        try:
            revision = int(row[0])
        except (TypeError, ValueError) as exc:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_REVISION_UNREADABLE",
                "stored binding revision is not an integer",
            ) from exc
        if revision < 0:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_REVISION_UNREADABLE",
                "stored binding revision is negative",
            )
        return revision

    def get_binding_revision(self) -> int:
        with self._transaction(write=False) as connection:
            return self._binding_revision(connection)

    def claim_runtime_request(
        self,
        request: Mapping[str, Any],
        *,
        expected_control_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Bind one runtime idempotency key to one normalized request.

        The claim deliberately does not advance either control revision.  It is
        used by the higher-level runtime before a multi-step Seed/Arc/Contract
        orchestration so that reusing one caller key for a different outline
        revision fails before any semantic control row is changed.
        """

        if not isinstance(request, Mapping):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_RUNTIME_REQUEST_REQUIRED",
                "runtime request must be an object",
            )
        normalized = normalize_for_hash(dict(request))

        def apply(_: sqlite3.Connection, __: int) -> dict[str, Any]:
            return {
                "request_hash": canonical_hash(normalized),
                "_advance_control_revision": False,
                "_advance_binding_revision": False,
            }

        return self._mutate(
            operation="ensure_locked_manifest_request",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={"runtime_request": normalized},
            apply=apply,
        )

    @staticmethod
    def _require_expected_revision(value: Any) -> int:
        return _require_int(value, "expected_control_revision")

    @staticmethod
    def _require_idempotency_key(value: Any) -> str:
        return _require_text(value, "idempotency_key", maximum=256)

    def _mutate(
        self,
        *,
        operation: str,
        idempotency_key: str,
        expected_control_revision: int,
        request: Mapping[str, Any],
        apply: Any,
    ) -> dict[str, Any]:
        operation = _require_text(operation, "operation", maximum=128)
        key = self._require_idempotency_key(idempotency_key)
        expected = self._require_expected_revision(expected_control_revision)
        request_hash = canonical_hash(
            {
                "operation": operation,
                "request": request,
            }
        )
        with self._transaction(write=True) as connection:
            previous = connection.execute(
                """
                SELECT request_hash, response_json
                FROM event_experience_idempotency
                WHERE operation=? AND idempotency_key=?
                """,
                (operation, key),
            ).fetchone()
            if previous is not None:
                if str(previous["request_hash"]) != request_hash:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_IDEMPOTENCY_CONFLICT",
                        "idempotency key was already used with a different request",
                        operation=operation,
                        idempotency_key=key,
                    )
                return json.loads(str(previous["response_json"]))

            actual = self._control_revision(connection)
            if actual != expected:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_STALE_CONTROL",
                    "event-experience control revision changed",
                    expected_control_revision=expected,
                    actual_control_revision=actual,
                )

            response = dict(apply(connection, actual))
            advance_revision = bool(
                response.pop("_advance_control_revision", True)
            )
            advance_binding_revision = bool(
                response.pop(
                    "_advance_binding_revision",
                    advance_revision,
                )
            )
            next_revision = actual
            if advance_revision:
                next_revision = actual + 1
                changed = connection.execute(
                    """
                    UPDATE event_experience_meta
                    SET value=?
                    WHERE key='control_revision' AND value=?
                    """,
                    (str(next_revision), str(actual)),
                ).rowcount
                if changed != 1:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_STALE_CONTROL",
                        "event-experience CAS lost its writer race",
                        expected_control_revision=actual,
                    )
            if advance_binding_revision:
                binding_revision = self._binding_revision(connection)
                changed = connection.execute(
                    """
                    UPDATE event_experience_meta
                    SET value=?
                    WHERE key='binding_revision' AND value=?
                    """,
                    (
                        str(binding_revision + 1),
                        str(binding_revision),
                    ),
                ).rowcount
                if changed != 1:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_STALE_CONTROL",
                        "event-experience binding CAS lost its writer race",
                        expected_binding_revision=binding_revision,
                    )
            response["control_revision"] = next_revision
            now = _utc_now()
            connection.execute(
                """
                INSERT INTO event_experience_idempotency(
                    operation, idempotency_key, request_hash,
                    response_json, created_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (operation, key, request_hash, _storage_json(response), now),
            )
            return response

    @staticmethod
    def _seed_payload(value: Any) -> dict[str, Any]:
        raw = _mapping_input(value, "EventSeed")
        _validate_supplied_schema(raw, "EventSeed")
        allowed = {
            "schema_version",
            "event_seed_id",
            "event_seed_revision",
            "parent_chain_id",
            "dependency_order",
            "dramatic_function",
            "causal_role",
            "intended_state_change",
            "event_boundary",
            "narrative_event_id",
            "artifact_id",
            "artifact_revision",
            "branch_id",
            "chapter_no",
            "scene_index",
            "source_outline_commit_id",
            "source_outline_artifact_version_id",
            "source_outline_artifact_id",
            "source_outline_artifact_revision",
            "source_outline_content_hash",
            "supersedes_seed_revision",
        }
        _reject_unknown(raw, allowed, "EventSeed")
        payload = {
            "schema_version": EVENT_EXPERIENCE_SCHEMA_VERSION,
            "event_seed_revision": _require_int(
                raw.get("event_seed_revision", 1),
                "event_seed_revision",
                minimum=1,
            ),
            "parent_chain_id": _require_text(
                raw.get("parent_chain_id"), "parent_chain_id", maximum=256
            ),
            "dependency_order": _require_int(
                raw.get("dependency_order"), "dependency_order"
            ),
            "dramatic_function": _require_text(
                raw.get("dramatic_function"), "dramatic_function"
            ),
            "causal_role": _require_text(raw.get("causal_role"), "causal_role"),
            "intended_state_change": _require_text(
                raw.get("intended_state_change"), "intended_state_change"
            ),
            "event_boundary": _require_text(
                raw.get("event_boundary"), "event_boundary"
            ),
            "narrative_event_id": _optional_text(
                raw.get("narrative_event_id"), "narrative_event_id", maximum=256
            ),
            "artifact_id": _optional_text(
                raw.get("artifact_id"), "artifact_id", maximum=256
            ),
            "artifact_revision": _require_int(
                raw.get("artifact_revision", 0), "artifact_revision"
            ),
            "branch_id": _require_text(
                raw.get("branch_id", "main"), "branch_id", maximum=256
            ),
            "chapter_no": (
                None
                if raw.get("chapter_no") is None
                else _require_int(raw.get("chapter_no"), "chapter_no")
            ),
            "scene_index": (
                None
                if raw.get("scene_index") is None
                else _require_int(raw.get("scene_index"), "scene_index")
            ),
            "source_outline_commit_id": _optional_text(
                raw.get("source_outline_commit_id"),
                "source_outline_commit_id",
                maximum=256,
            ),
            "source_outline_artifact_version_id": _optional_text(
                raw.get("source_outline_artifact_version_id"),
                "source_outline_artifact_version_id",
                maximum=256,
            ),
            "source_outline_artifact_id": _optional_text(
                raw.get("source_outline_artifact_id"),
                "source_outline_artifact_id",
                maximum=256,
            ),
            "source_outline_artifact_revision": _require_int(
                raw.get("source_outline_artifact_revision", 0),
                "source_outline_artifact_revision",
            ),
            "source_outline_content_hash": (
                ""
                if not raw.get("source_outline_content_hash")
                else _require_sha256(
                    raw.get("source_outline_content_hash"),
                    "source_outline_content_hash",
                )
            ),
        }
        outline_values = (
            payload["source_outline_commit_id"],
            payload["source_outline_artifact_version_id"],
            payload["source_outline_artifact_id"],
            payload["source_outline_artifact_revision"],
            payload["source_outline_content_hash"],
        )
        if any(outline_values) and not all(outline_values):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_OUTLINE_BINDING_INCOMPLETE",
                "accepted outline binding must contain the full identity tuple",
            )
        if raw.get("supersedes_seed_revision") is not None:
            payload["supersedes_seed_revision"] = _require_int(
                raw.get("supersedes_seed_revision"),
                "supersedes_seed_revision",
                minimum=1,
            )
        event_seed_id = _optional_text(
            raw.get("event_seed_id"), "event_seed_id", maximum=256
        )
        payload["event_seed_id"] = event_seed_id or _stable_id(
            "event-seed",
            {
                key: item
                for key, item in payload.items()
                if key not in {"event_seed_revision", "supersedes_seed_revision"}
            },
        )
        return normalize_for_hash(payload)

    @staticmethod
    def _seed_from_row(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(str(row["payload_json"]))
        payload.update(
            {
                "seed_hash": str(row["seed_hash"]),
                "status": str(row["status"]),
                "experience_contract_id": row["experience_contract_id"],
                "experience_contract_hash": row["experience_contract_hash"],
                "retired_at": row["retired_at"],
                "retired_reason": row["retired_reason"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
        )
        return payload

    def create_seed(
        self,
        seed: EventSeed | Mapping[str, Any],
        *,
        expected_control_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = self._seed_payload(seed)
        if payload["event_seed_revision"] != 1:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_SEED_REVISION",
                "new EventSeed must begin at revision 1",
            )
        seed_hash = canonical_hash(payload)

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            occupied = connection.execute(
                """
                SELECT event_seed_id, event_seed_revision
                FROM event_seeds
                WHERE parent_chain_id=? AND dependency_order=?
                  AND status!='retired'
                LIMIT 1
                """,
                (
                    payload["parent_chain_id"],
                    payload["dependency_order"],
                ),
            ).fetchone()
            if occupied is not None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_DEPENDENCY_ORDER_CONFLICT",
                    "parent chain already has an active EventSeed at this dependency order",
                    event_seed_id=str(occupied["event_seed_id"]),
                    event_seed_revision=int(
                        occupied["event_seed_revision"]
                    ),
                    dependency_order=payload["dependency_order"],
                )
            now = _utc_now()
            try:
                connection.execute(
                    """
                    INSERT INTO event_seeds(
                        event_seed_id, event_seed_revision, parent_chain_id,
                        dependency_order, seed_hash, payload_json, status,
                        supersedes_seed_revision, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, 'seeded', NULL, ?, ?)
                    """,
                    (
                        payload["event_seed_id"],
                        payload["event_seed_revision"],
                        payload["parent_chain_id"],
                        payload["dependency_order"],
                        seed_hash,
                        canonical_json(payload),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_EXISTS",
                    "EventSeed identity already exists",
                    event_seed_id=payload["event_seed_id"],
                    event_seed_revision=payload["event_seed_revision"],
                ) from exc
            row = self._fetch_seed_row(
                connection,
                payload["event_seed_id"],
                payload["event_seed_revision"],
            )
            return {"seed": self._seed_from_row(row)}

        return self._mutate(
            operation="create_seed",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={"seed": payload},
            apply=apply,
        )

    @staticmethod
    def _fetch_seed_row(
        connection: sqlite3.Connection,
        event_seed_id: str,
        event_seed_revision: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM event_seeds
            WHERE event_seed_id=? AND event_seed_revision=?
            """,
            (event_seed_id, event_seed_revision),
        ).fetchone()
        if row is None:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_SEED_NOT_FOUND",
                "EventSeed was not found",
                event_seed_id=event_seed_id,
                event_seed_revision=event_seed_revision,
            )
        return row

    def get_seed(
        self,
        event_seed_id: str,
        event_seed_revision: int | None = None,
    ) -> dict[str, Any]:
        seed_id = _require_text(event_seed_id, "event_seed_id", maximum=256)
        with self._transaction(write=False) as connection:
            if event_seed_revision is None:
                row = connection.execute(
                    """
                    SELECT * FROM event_seeds
                    WHERE event_seed_id=?
                    ORDER BY event_seed_revision DESC LIMIT 1
                    """,
                    (seed_id,),
                ).fetchone()
                if row is None:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_SEED_NOT_FOUND",
                        "EventSeed was not found",
                        event_seed_id=seed_id,
                    )
            else:
                row = self._fetch_seed_row(
                    connection,
                    seed_id,
                    _require_int(
                        event_seed_revision,
                        "event_seed_revision",
                        minimum=1,
                    ),
                )
            return self._seed_from_row(row)

    def supersede_seed(
        self,
        event_seed_id: str,
        replacement: EventSeed | Mapping[str, Any],
        *,
        expected_control_revision: int,
        idempotency_key: str,
        reason: str,
    ) -> dict[str, Any]:
        seed_id = _require_text(event_seed_id, "event_seed_id", maximum=256)
        reason_text = _require_text(reason, "reason")
        raw = _mapping_input(replacement, "EventSeed")

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            previous = connection.execute(
                """
                SELECT * FROM event_seeds
                WHERE event_seed_id=?
                ORDER BY event_seed_revision DESC LIMIT 1
                """,
                (seed_id,),
            ).fetchone()
            if previous is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_NOT_FOUND",
                    "EventSeed was not found",
                    event_seed_id=seed_id,
                )
            if str(previous["status"]) == "retired":
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_RETIRED",
                    "retired EventSeed cannot be superseded",
                )
            previous_revision = int(previous["event_seed_revision"])
            supplied_supersedes = raw.get("supersedes_seed_revision")
            if (
                supplied_supersedes is not None
                and supplied_supersedes != previous_revision
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_SUPERSESSION_MISMATCH",
                    "replacement supersedes_seed_revision does not match the latest EventSeed",
                    expected_seed_revision=previous_revision,
                    supplied_seed_revision=supplied_supersedes,
                )
            candidate = dict(raw)
            candidate["event_seed_id"] = seed_id
            candidate["event_seed_revision"] = previous_revision + 1
            candidate["supersedes_seed_revision"] = previous_revision
            payload = self._seed_payload(candidate)
            previous_payload = json.loads(str(previous["payload_json"]))
            for field in ("parent_chain_id", "branch_id", "artifact_id"):
                if payload.get(field) != previous_payload.get(field):
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_SEED_IDENTITY_MISMATCH",
                        f"EventSeed supersession cannot change {field}",
                        field=field,
                        previous=previous_payload.get(field),
                        replacement=payload.get(field),
                    )
            seed_hash = canonical_hash(payload)
            occupied = connection.execute(
                """
                SELECT event_seed_id, event_seed_revision
                FROM event_seeds
                WHERE parent_chain_id=? AND dependency_order=?
                  AND status!='retired' AND event_seed_id!=?
                LIMIT 1
                """,
                (
                    payload["parent_chain_id"],
                    payload["dependency_order"],
                    seed_id,
                ),
            ).fetchone()
            if occupied is not None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_DEPENDENCY_ORDER_CONFLICT",
                    "parent chain already has another active EventSeed at this dependency order",
                    event_seed_id=str(occupied["event_seed_id"]),
                    event_seed_revision=int(
                        occupied["event_seed_revision"]
                    ),
                    dependency_order=payload["dependency_order"],
                )
            now = _utc_now()
            connection.execute(
                """
                UPDATE event_seeds
                SET status='retired', retired_at=?, retired_reason=?, updated_at=?
                WHERE event_seed_id=? AND event_seed_revision=?
                """,
                (now, reason_text, now, seed_id, previous_revision),
            )
            connection.execute(
                """
                UPDATE event_experience_contracts
                SET status='retired', retired_at=?, retired_reason=?, updated_at=?
                WHERE event_seed_id=? AND event_seed_revision=?
                  AND status IN ('proposed', 'locked')
                """,
                (now, reason_text, now, seed_id, previous_revision),
            )
            connection.execute(
                """
                INSERT INTO event_seeds(
                    event_seed_id, event_seed_revision, parent_chain_id,
                    dependency_order, seed_hash, payload_json, status,
                    supersedes_seed_revision, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'seeded', ?, ?, ?)
                """,
                (
                    seed_id,
                    payload["event_seed_revision"],
                    payload["parent_chain_id"],
                    payload["dependency_order"],
                    seed_hash,
                    canonical_json(payload),
                    previous_revision,
                    now,
                    now,
                ),
            )
            row = self._fetch_seed_row(
                connection, seed_id, payload["event_seed_revision"]
            )
            return {
                "seed": self._seed_from_row(row),
                "superseded_seed_revision": previous_revision,
            }

        return self._mutate(
            operation="supersede_seed",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={
                "event_seed_id": seed_id,
                "replacement": raw,
                "reason": reason_text,
            },
            apply=apply,
        )

    @staticmethod
    def _arc_payload(value: Any) -> dict[str, Any]:
        raw = _mapping_input(value, "EventExperienceArc")
        _validate_supplied_schema(raw, "EventExperienceArc")
        allowed = {
            "schema_version",
            "arc_id",
            "arc_revision",
            "parent_chain_id",
            "entry_reader_state",
            "target_reader_state",
            "overall_peak",
            "release_rhythm",
            "aftertaste",
            "event_seed_ids",
            "branch_id",
            "artifact_id",
            "artifact_revision",
            "supersedes_arc_revision",
        }
        _reject_unknown(raw, allowed, "EventExperienceArc")
        payload = {
            "schema_version": EVENT_EXPERIENCE_SCHEMA_VERSION,
            "arc_revision": _require_int(
                raw.get("arc_revision", 1), "arc_revision", minimum=1
            ),
            "parent_chain_id": _require_text(
                raw.get("parent_chain_id"), "parent_chain_id", maximum=256
            ),
            "entry_reader_state": _require_text(
                raw.get("entry_reader_state"), "entry_reader_state"
            ),
            "target_reader_state": _require_text(
                raw.get("target_reader_state"), "target_reader_state"
            ),
            "overall_peak": _require_text(
                raw.get("overall_peak"), "overall_peak"
            ),
            "release_rhythm": _require_text(
                raw.get("release_rhythm"), "release_rhythm"
            ),
            "aftertaste": _require_text(raw.get("aftertaste"), "aftertaste"),
            "event_seed_ids": _text_list(
                raw.get("event_seed_ids", []),
                "event_seed_ids",
                required=True,
            ),
            "branch_id": _require_text(
                raw.get("branch_id", "main"), "branch_id", maximum=256
            ),
            "artifact_id": _optional_text(
                raw.get("artifact_id"), "artifact_id", maximum=256
            ),
            "artifact_revision": _require_int(
                raw.get("artifact_revision", 0), "artifact_revision"
            ),
        }
        if len(set(payload["event_seed_ids"])) != len(payload["event_seed_ids"]):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_DUPLICATE_SEED",
                "event_seed_ids must be unique",
            )
        if raw.get("supersedes_arc_revision") is not None:
            payload["supersedes_arc_revision"] = _require_int(
                raw.get("supersedes_arc_revision"),
                "supersedes_arc_revision",
                minimum=1,
            )
        arc_id = _optional_text(raw.get("arc_id"), "arc_id", maximum=256)
        payload["arc_id"] = arc_id or _stable_id(
            "experience-arc",
            {
                "parent_chain_id": payload["parent_chain_id"],
                "branch_id": payload["branch_id"],
                "artifact_id": payload["artifact_id"],
            },
        )
        return normalize_for_hash(payload)

    @staticmethod
    def _arc_from_row(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(str(row["payload_json"]))
        payload.update(
            {
                "arc_hash": str(row["arc_hash"]),
                "status": str(row["status"]),
                "locked_at": row["locked_at"],
                "retired_at": row["retired_at"],
                "retired_reason": row["retired_reason"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
        )
        return payload

    @staticmethod
    def _bind_arc_seeds(
        connection: sqlite3.Connection,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        bound = dict(payload)
        bindings: list[dict[str, Any]] = []
        for seed_id in payload["event_seed_ids"]:
            row = connection.execute(
                """
                SELECT event_seed_id, event_seed_revision, seed_hash,
                       parent_chain_id, status, payload_json
                FROM event_seeds
                WHERE event_seed_id=? AND status!='retired'
                ORDER BY event_seed_revision DESC LIMIT 1
                """,
                (seed_id,),
            ).fetchone()
            if (
                row is None
                or str(row["parent_chain_id"])
                != str(payload["parent_chain_id"])
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_SEED_MISMATCH",
                    "arc references a missing or foreign EventSeed",
                    event_seed_id=seed_id,
                )
            seed_payload = json.loads(str(row["payload_json"]))
            for field, expected in (
                ("branch_id", str(payload.get("branch_id", "main"))),
                ("artifact_id", str(payload.get("artifact_id", ""))),
                (
                    "artifact_revision",
                    int(payload.get("artifact_revision", 0)),
                ),
            ):
                observed = seed_payload.get(
                    field,
                    "main"
                    if field == "branch_id"
                    else (0 if field == "artifact_revision" else ""),
                )
                if observed != expected:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_ARC_SEED_CONTEXT_MISMATCH",
                        "arc and EventSeed must share branch and artifact revision",
                        event_seed_id=seed_id,
                        field=field,
                        expected=expected,
                        observed=observed,
                    )
            bindings.append(
                {
                    "event_seed_id": str(row["event_seed_id"]),
                    "event_seed_revision": int(row["event_seed_revision"]),
                    "seed_hash": str(row["seed_hash"]),
                }
            )
        bound["event_seed_bindings"] = bindings
        return normalize_for_hash(bound)

    @staticmethod
    def _validate_arc_seed_bindings(
        connection: sqlite3.Connection,
        payload: Mapping[str, Any],
    ) -> None:
        bindings = payload.get("event_seed_bindings")
        if not isinstance(bindings, list) or len(bindings) != len(
            payload.get("event_seed_ids", [])
        ):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_ARC_BINDING_MISSING",
                "arc lacks immutable EventSeed revision/hash bindings",
            )
        for index, binding in enumerate(bindings):
            if not isinstance(binding, Mapping):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_BINDING_INVALID",
                    "arc EventSeed binding must be an object",
                    index=index,
                )
            seed_id = _require_text(
                binding.get("event_seed_id"),
                f"event_seed_bindings[{index}].event_seed_id",
                maximum=256,
            )
            seed_revision = _require_int(
                binding.get("event_seed_revision"),
                f"event_seed_bindings[{index}].event_seed_revision",
                minimum=1,
            )
            seed_hash = _require_sha256(
                binding.get("seed_hash"),
                f"event_seed_bindings[{index}].seed_hash",
            )
            if seed_id != payload["event_seed_ids"][index]:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_BINDING_INVALID",
                    "arc EventSeed binding order does not match event_seed_ids",
                    index=index,
                )
            row = connection.execute(
                """
                SELECT seed_hash, parent_chain_id, status
                FROM event_seeds
                WHERE event_seed_id=? AND event_seed_revision=?
                """,
                (seed_id, seed_revision),
            ).fetchone()
            if (
                row is None
                or str(row["status"]) == "retired"
                or str(row["seed_hash"]) != seed_hash
                or str(row["parent_chain_id"])
                != str(payload["parent_chain_id"])
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_BINDING_STALE",
                    "arc EventSeed revision/hash binding is no longer active",
                    event_seed_id=seed_id,
                    event_seed_revision=seed_revision,
                )

    def create_arc(
        self,
        arc: EventExperienceArc | Mapping[str, Any],
        *,
        expected_control_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = self._arc_payload(arc)
        if payload["arc_revision"] != 1:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_ARC_REVISION",
                "new EventExperienceArc must begin at revision 1",
            )
        arc_hash = canonical_hash(payload)

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            stored_payload = self._bind_arc_seeds(connection, payload)
            stored_arc_hash = canonical_hash(stored_payload)
            active_rows = connection.execute(
                """
                SELECT arc_id, arc_revision, payload_json
                FROM event_experience_arcs
                WHERE parent_chain_id=?
                  AND status IN ('proposed', 'locked')
                """,
                (payload["parent_chain_id"],),
            ).fetchall()
            for active in active_rows:
                active_payload = json.loads(str(active["payload_json"]))
                if (
                    str(active_payload.get("branch_id", "main"))
                    == payload["branch_id"]
                    and str(active_payload.get("artifact_id", ""))
                    == payload["artifact_id"]
                    and int(active_payload.get("artifact_revision", 0))
                    == payload["artifact_revision"]
                ):
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_ARC_ACTIVE",
                        "chain, branch, and artifact already have an active arc",
                        arc_id=str(active["arc_id"]),
                        arc_revision=int(active["arc_revision"]),
                    )
            now = _utc_now()
            try:
                connection.execute(
                    """
                    INSERT INTO event_experience_arcs(
                        arc_id, arc_revision, parent_chain_id, arc_hash,
                        payload_json, status, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, 'proposed', ?, ?)
                    """,
                    (
                        payload["arc_id"],
                        payload["arc_revision"],
                        payload["parent_chain_id"],
                        stored_arc_hash,
                        canonical_json(stored_payload),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_EXISTS",
                    "EventExperienceArc identity already exists",
                ) from exc
            row = connection.execute(
                """
                SELECT * FROM event_experience_arcs
                WHERE arc_id=? AND arc_revision=?
                """,
                (payload["arc_id"], payload["arc_revision"]),
            ).fetchone()
            return {"arc": self._arc_from_row(row)}

        return self._mutate(
            operation="create_arc",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={"arc": payload},
            apply=apply,
        )

    def get_arc(
        self,
        arc_id: str,
        arc_revision: int | None = None,
    ) -> dict[str, Any]:
        arc_key = _require_text(arc_id, "arc_id", maximum=256)
        with self._transaction(write=False) as connection:
            if arc_revision is None:
                row = connection.execute(
                    """
                    SELECT * FROM event_experience_arcs
                    WHERE arc_id=?
                    ORDER BY arc_revision DESC LIMIT 1
                    """,
                    (arc_key,),
                ).fetchone()
            else:
                revision = _require_int(
                    arc_revision, "arc_revision", minimum=1
                )
                row = connection.execute(
                    """
                    SELECT * FROM event_experience_arcs
                    WHERE arc_id=? AND arc_revision=?
                    """,
                    (arc_key, revision),
                ).fetchone()
            if row is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_NOT_FOUND",
                    "EventExperienceArc was not found",
                    arc_id=arc_key,
                    arc_revision=arc_revision,
                )
            return self._arc_from_row(row)

    def lock_arc(
        self,
        arc_id: str,
        arc_revision: int,
        *,
        expected_control_revision: int,
        idempotency_key: str,
        expected_arc_hash: str | None = None,
    ) -> dict[str, Any]:
        arc_key = _require_text(arc_id, "arc_id", maximum=256)
        revision = _require_int(arc_revision, "arc_revision", minimum=1)
        expected_hash = (
            ""
            if expected_arc_hash is None
            else _require_sha256(expected_arc_hash, "expected_arc_hash")
        )

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            row = connection.execute(
                """
                SELECT * FROM event_experience_arcs
                WHERE arc_id=? AND arc_revision=?
                """,
                (arc_key, revision),
            ).fetchone()
            if row is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_NOT_FOUND",
                    "EventExperienceArc was not found",
                )
            if str(row["status"]) != "proposed":
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_NOT_PROPOSED",
                    "only a proposed arc can be locked",
                    status=str(row["status"]),
                )
            if expected_hash and str(row["arc_hash"]) != expected_hash:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_HASH_MISMATCH",
                    "arc hash changed before lock",
                )
            self._validate_arc_seed_bindings(
                connection, json.loads(str(row["payload_json"]))
            )
            now = _utc_now()
            connection.execute(
                """
                UPDATE event_experience_arcs
                SET status='locked', locked_at=?, updated_at=?
                WHERE arc_id=? AND arc_revision=? AND status='proposed'
                """,
                (now, now, arc_key, revision),
            )
            locked = connection.execute(
                """
                SELECT * FROM event_experience_arcs
                WHERE arc_id=? AND arc_revision=?
                """,
                (arc_key, revision),
            ).fetchone()
            return {"arc": self._arc_from_row(locked)}

        return self._mutate(
            operation="lock_arc",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={
                "arc_id": arc_key,
                "arc_revision": revision,
                "expected_arc_hash": expected_hash,
            },
            apply=apply,
        )

    def supersede_arc(
        self,
        arc_id: str,
        replacement: EventExperienceArc | Mapping[str, Any],
        *,
        expected_control_revision: int,
        idempotency_key: str,
        reason: str,
        lock_replacement: bool = False,
    ) -> dict[str, Any]:
        arc_key = _require_text(arc_id, "arc_id", maximum=256)
        reason_text = _require_text(reason, "reason")
        if not isinstance(lock_replacement, bool):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_BOOLEAN_REQUIRED",
                "lock_replacement must be boolean",
            )
        raw = _mapping_input(replacement, "EventExperienceArc")

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            previous = connection.execute(
                """
                SELECT * FROM event_experience_arcs
                WHERE arc_id=?
                ORDER BY arc_revision DESC LIMIT 1
                """,
                (arc_key,),
            ).fetchone()
            if previous is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_NOT_FOUND",
                    "EventExperienceArc was not found",
                    arc_id=arc_key,
                )
            if str(previous["status"]) == "retired":
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_RETIRED",
                    "retired EventExperienceArc cannot be superseded",
                    arc_id=arc_key,
                    arc_revision=int(previous["arc_revision"]),
                )
            previous_payload = json.loads(str(previous["payload_json"]))
            supplied_supersedes = raw.get("supersedes_arc_revision")
            if (
                supplied_supersedes is not None
                and supplied_supersedes != int(previous["arc_revision"])
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_SUPERSESSION_MISMATCH",
                    "replacement supersedes_arc_revision does not match the latest arc",
                    expected_arc_revision=int(previous["arc_revision"]),
                    supplied_arc_revision=supplied_supersedes,
                )
            candidate = dict(raw)
            candidate["arc_id"] = arc_key
            candidate["arc_revision"] = int(previous["arc_revision"]) + 1
            candidate["supersedes_arc_revision"] = int(
                previous["arc_revision"]
            )
            payload = self._arc_payload(candidate)
            if (
                payload["parent_chain_id"]
                != previous_payload["parent_chain_id"]
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_CHAIN_MISMATCH",
                    "arc supersession cannot change parent_chain_id",
                    previous_parent_chain_id=previous_payload[
                        "parent_chain_id"
                    ],
                    replacement_parent_chain_id=payload["parent_chain_id"],
                )
            stored_payload = self._bind_arc_seeds(connection, payload)
            arc_hash = canonical_hash(stored_payload)
            now = _utc_now()
            connection.execute(
                """
                UPDATE event_experience_arcs
                SET status='retired', retired_at=?, retired_reason=?,
                    updated_at=?
                WHERE arc_id=? AND arc_revision=?
                """,
                (
                    now,
                    reason_text,
                    now,
                    arc_key,
                    int(previous["arc_revision"]),
                ),
            )
            status = "locked" if lock_replacement else "proposed"
            connection.execute(
                """
                INSERT INTO event_experience_arcs(
                    arc_id, arc_revision, parent_chain_id, arc_hash,
                    payload_json, status, supersedes_arc_revision,
                    locked_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    arc_key,
                    payload["arc_revision"],
                    payload["parent_chain_id"],
                    arc_hash,
                    canonical_json(stored_payload),
                    status,
                    int(previous["arc_revision"]),
                    now if lock_replacement else None,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM event_experience_arcs
                WHERE arc_id=? AND arc_revision=?
                """,
                (arc_key, payload["arc_revision"]),
            ).fetchone()
            return {
                "arc": self._arc_from_row(row),
                "superseded_arc_revision": int(previous["arc_revision"]),
            }

        return self._mutate(
            operation="supersede_arc",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={
                "arc_id": arc_key,
                "replacement": raw,
                "reason": reason_text,
                "lock_replacement": lock_replacement,
            },
            apply=apply,
        )

    def retire_arc(
        self,
        arc_id: str,
        arc_revision: int,
        *,
        expected_control_revision: int,
        idempotency_key: str,
        reason: str,
    ) -> dict[str, Any]:
        arc_key = _require_text(arc_id, "arc_id", maximum=256)
        revision = _require_int(arc_revision, "arc_revision", minimum=1)
        reason_text = _require_text(reason, "reason")

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            row = connection.execute(
                """
                SELECT * FROM event_experience_arcs
                WHERE arc_id=? AND arc_revision=?
                """,
                (arc_key, revision),
            ).fetchone()
            if row is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_NOT_FOUND",
                    "EventExperienceArc was not found",
                    arc_id=arc_key,
                    arc_revision=revision,
                )
            if str(row["status"]) == "retired":
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_RETIRED",
                    "EventExperienceArc is already retired",
                    arc_id=arc_key,
                    arc_revision=revision,
                )
            newer = connection.execute(
                """
                SELECT 1 FROM event_experience_arcs
                WHERE arc_id=? AND arc_revision>?
                LIMIT 1
                """,
                (arc_key, revision),
            ).fetchone()
            if newer is not None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_ARC_NOT_LATEST",
                    "only the latest EventExperienceArc revision can be retired",
                    arc_id=arc_key,
                    arc_revision=revision,
                )
            now = _utc_now()
            connection.execute(
                """
                UPDATE event_experience_arcs
                SET status='retired', retired_at=?, retired_reason=?,
                    updated_at=?
                WHERE arc_id=? AND arc_revision=?
                """,
                (now, reason_text, now, arc_key, revision),
            )
            retired = connection.execute(
                """
                SELECT * FROM event_experience_arcs
                WHERE arc_id=? AND arc_revision=?
                """,
                (arc_key, revision),
            ).fetchone()
            return {"arc": self._arc_from_row(retired)}

        return self._mutate(
            operation="retire_arc",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={
                "arc_id": arc_key,
                "arc_revision": revision,
                "reason": reason_text,
            },
            apply=apply,
        )

    @staticmethod
    def _contract_payload(
        value: Any,
        *,
        seed_payload: Mapping[str, Any],
        supersedes_contract_id: str = "",
        contract_revision: int | None = None,
    ) -> dict[str, Any]:
        raw = _mapping_input(value, "EventExperienceContract")
        _validate_supplied_schema(raw, "EventExperienceContract")
        allowed = {
            "schema_version",
            "contract_id",
            "contract_revision",
            "event_seed_id",
            "event_seed_revision",
            "parent_chain_id",
            "narrative_event_id",
            "artifact_id",
            "artifact_revision",
            "branch_id",
            "chapter_no",
            "scene_index",
            "source_intent_contract_id",
            "source_intent_contract_revision",
            "source_intent_contract_hash",
            "supersedes_contract_id",
            "dramatic_function",
            "event_boundary",
            "entry_reader_state",
            "target_reader_state",
            "primary_emotion",
            "ordered_secondary_emotions",
            "emotional_turn",
            "intensity",
            "emotion_curve",
            "mechanisms",
            "reader_knowledge_position",
            "viewpoint_character_state",
            "payoff_or_reveal",
            "aftertaste",
            "anti_experiences",
            "success_signals",
            "open_loop_links",
            "derivation",
            "field_provenance",
        }
        _reject_unknown(raw, allowed, "EventExperienceContract")
        seed_id = str(seed_payload["event_seed_id"])
        seed_revision = int(seed_payload["event_seed_revision"])
        supplied_seed_id = raw.get("event_seed_id", seed_id)
        supplied_seed_revision = raw.get("event_seed_revision", seed_revision)
        if supplied_seed_id != seed_id or supplied_seed_revision != seed_revision:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_CONTRACT_SEED_MISMATCH",
                "contract identity does not match its EventSeed",
            )
        text_bindings = {
            "parent_chain_id": str(seed_payload["parent_chain_id"]),
            "narrative_event_id": str(
                seed_payload.get("narrative_event_id", "")
            ),
            "artifact_id": str(seed_payload.get("artifact_id", "")),
            "branch_id": str(seed_payload.get("branch_id", "main")),
            "dramatic_function": str(seed_payload["dramatic_function"]),
            "event_boundary": str(seed_payload["event_boundary"]),
        }
        for field, expected_value in text_bindings.items():
            if field not in raw:
                continue
            supplied_value = (
                _optional_text(raw.get(field), field, maximum=4096)
                if expected_value == ""
                else _require_text(raw.get(field), field, maximum=4096)
            )
            if supplied_value != expected_value:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_SEED_CONTEXT_MISMATCH",
                    f"contract {field} does not match its EventSeed",
                    field=field,
                    expected=expected_value,
                    supplied=supplied_value,
                )
        integer_bindings = {
            "artifact_revision": int(
                seed_payload.get("artifact_revision", 0)
            ),
            "chapter_no": seed_payload.get("chapter_no"),
            "scene_index": seed_payload.get("scene_index"),
        }
        for field, expected_value in integer_bindings.items():
            if field not in raw:
                continue
            supplied_raw = raw.get(field)
            supplied_value = (
                None
                if supplied_raw is None
                else _require_int(supplied_raw, field)
            )
            if supplied_value != expected_value:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_SEED_CONTEXT_MISMATCH",
                    f"contract {field} does not match its EventSeed",
                    field=field,
                    expected=expected_value,
                    supplied=supplied_value,
                )
        intensity_raw = raw.get("intensity")
        if not isinstance(intensity_raw, Mapping):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_INTENSITY_REQUIRED",
                "intensity must contain entry, peak, and exit",
            )
        if set(intensity_raw) != {"entry", "peak", "exit"}:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_INTENSITY_SHAPE",
                "intensity must contain exactly entry, peak, and exit",
            )
        intensity = {
            field: _require_int(
                intensity_raw[field],
                f"intensity.{field}",
                minimum=0,
                maximum=100,
            )
            for field in ("entry", "peak", "exit")
        }
        if intensity["peak"] < max(intensity["entry"], intensity["exit"]):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_INTENSITY_PEAK",
                "peak intensity must not be lower than entry or exit",
            )
        derivation_raw = raw.get("derivation")
        if not isinstance(derivation_raw, Mapping):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_DERIVATION_REQUIRED",
                "derivation must be an object",
            )
        confidence = derivation_raw.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_CONFIDENCE_REQUIRED",
                "derivation.confidence must be a number from 0 to 1",
            )
        confidence_number = float(confidence)
        if not math.isfinite(confidence_number) or not 0 <= confidence_number <= 1:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_CONFIDENCE_RANGE",
                "derivation.confidence must be a number from 0 to 1",
            )
        user_confirmed = derivation_raw.get("user_confirmed", False)
        delegated_choice = derivation_raw.get("delegated_choice", False)
        if not isinstance(user_confirmed, bool) or not isinstance(
            delegated_choice, bool
        ):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_DERIVATION_BOOLEAN",
                "derivation confirmation fields must be booleans",
            )
        field_provenance = raw.get("field_provenance", {})
        if field_provenance is None:
            field_provenance = {}
        if not isinstance(field_provenance, Mapping):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_PROVENANCE_REQUIRED",
                "field_provenance must be an object",
            )
        revision = (
            contract_revision
            if contract_revision is not None
            else _require_int(
                raw.get("contract_revision", 1),
                "contract_revision",
                minimum=1,
            )
        )
        payload: dict[str, Any] = {
            "schema_version": EVENT_EXPERIENCE_SCHEMA_VERSION,
            "contract_revision": revision,
            "event_seed_id": seed_id,
            "event_seed_revision": seed_revision,
            "parent_chain_id": str(seed_payload["parent_chain_id"]),
            "narrative_event_id": str(
                seed_payload.get("narrative_event_id", "")
            ),
            "artifact_id": str(seed_payload.get("artifact_id", "")),
            "artifact_revision": int(
                seed_payload.get("artifact_revision", 0)
            ),
            "branch_id": str(seed_payload.get("branch_id", "main")),
            "chapter_no": seed_payload.get("chapter_no"),
            "scene_index": seed_payload.get("scene_index"),
            "source_intent_contract_id": _require_text(
                raw.get("source_intent_contract_id"),
                "source_intent_contract_id",
                maximum=256,
            ),
            "source_intent_contract_revision": _require_int(
                raw.get("source_intent_contract_revision"),
                "source_intent_contract_revision",
                minimum=1,
            ),
            "source_intent_contract_hash": _require_sha256(
                raw.get("source_intent_contract_hash"),
                "source_intent_contract_hash",
            ),
            "supersedes_contract_id": (
                supersedes_contract_id
                or _optional_text(
                    raw.get("supersedes_contract_id"),
                    "supersedes_contract_id",
                    maximum=256,
                )
            ),
            "dramatic_function": str(seed_payload["dramatic_function"]),
            "event_boundary": str(seed_payload["event_boundary"]),
            "entry_reader_state": _require_text(
                raw.get("entry_reader_state"), "entry_reader_state"
            ),
            "target_reader_state": _require_text(
                raw.get("target_reader_state"), "target_reader_state"
            ),
            "primary_emotion": _require_text(
                raw.get("primary_emotion"), "primary_emotion"
            ),
            "ordered_secondary_emotions": _text_list(
                raw.get("ordered_secondary_emotions", []),
                "ordered_secondary_emotions",
            ),
            "emotional_turn": _require_text(
                raw.get("emotional_turn"), "emotional_turn"
            ),
            "intensity": intensity,
            "emotion_curve": _text_list(
                raw.get("emotion_curve", []),
                "emotion_curve",
                required=True,
            ),
            "mechanisms": _text_list(
                raw.get("mechanisms", []), "mechanisms", required=True
            ),
            "reader_knowledge_position": _require_text(
                raw.get("reader_knowledge_position"),
                "reader_knowledge_position",
            ),
            "viewpoint_character_state": _require_text(
                raw.get("viewpoint_character_state"),
                "viewpoint_character_state",
            ),
            "payoff_or_reveal": _require_text(
                raw.get("payoff_or_reveal"), "payoff_or_reveal"
            ),
            "aftertaste": _require_text(raw.get("aftertaste"), "aftertaste"),
            "anti_experiences": _text_list(
                raw.get("anti_experiences", []), "anti_experiences"
            ),
            "success_signals": _text_list(
                raw.get("success_signals", []),
                "success_signals",
                required=True,
            ),
            "open_loop_links": _text_list(
                raw.get("open_loop_links", []), "open_loop_links"
            ),
            "derivation": {
                "source": _require_text(
                    derivation_raw.get("source"), "derivation.source"
                ),
                "confidence": confidence_number,
                "user_confirmed": user_confirmed,
                "delegated_choice": delegated_choice,
            },
            "field_provenance": normalize_for_hash(field_provenance),
        }
        contract_id = _optional_text(
            raw.get("contract_id"), "contract_id", maximum=256
        )
        payload["contract_id"] = contract_id or _stable_id(
            "experience-contract",
            payload,
        )
        return normalize_for_hash(payload)

    @staticmethod
    def _contract_from_row(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(str(row["payload_json"]))
        payload.update(
            {
                "contract_hash": str(row["contract_hash"]),
                "status": str(row["status"]),
                "locked_at": row["locked_at"],
                "retired_at": row["retired_at"],
                "retired_reason": row["retired_reason"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
        )
        return payload

    @staticmethod
    def _validate_lockable_contract_payload(
        payload: Mapping[str, Any],
    ) -> None:
        _require_text(
            payload.get("source_intent_contract_id"),
            "source_intent_contract_id",
            maximum=256,
        )
        _require_int(
            payload.get("source_intent_contract_revision"),
            "source_intent_contract_revision",
            minimum=1,
        )
        _require_sha256(
            payload.get("source_intent_contract_hash"),
            "source_intent_contract_hash",
        )
        provenance = payload.get("field_provenance")
        if not isinstance(provenance, Mapping):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_PROVENANCE_REQUIRED",
                "locked contract field_provenance must be an object",
            )
        missing = sorted(_LOCKED_PROVENANCE_FIELDS - set(provenance))
        if missing:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_PROVENANCE_INCOMPLETE",
                "locked contract lacks provenance for required experience fields",
                missing_fields=missing,
            )
        for field in sorted(_LOCKED_PROVENANCE_FIELDS):
            evidence = provenance[field]
            if not isinstance(evidence, Mapping) or not evidence:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_PROVENANCE_INVALID",
                    "locked contract provenance entries must be non-empty objects",
                    field=field,
                )

    def propose_contract(
        self,
        contract: EventExperienceContract | Mapping[str, Any],
        *,
        expected_control_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        raw = _mapping_input(contract, "EventExperienceContract")
        seed_id = _require_text(
            raw.get("event_seed_id"), "event_seed_id", maximum=256
        )
        seed_revision = _require_int(
            raw.get("event_seed_revision"), "event_seed_revision", minimum=1
        )

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            seed_row = self._fetch_seed_row(connection, seed_id, seed_revision)
            if str(seed_row["status"]) == "retired":
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_RETIRED",
                    "retired EventSeed cannot receive a contract",
                )
            existing = connection.execute(
                """
                SELECT contract_id FROM event_experience_contracts
                WHERE event_seed_id=? AND event_seed_revision=?
                  AND status IN ('proposed', 'locked')
                """,
                (seed_id, seed_revision),
            ).fetchone()
            if existing is not None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_ACTIVE",
                    "EventSeed already has an active contract",
                    contract_id=str(existing["contract_id"]),
                )
            seed_payload = json.loads(str(seed_row["payload_json"]))
            payload = self._contract_payload(raw, seed_payload=seed_payload)
            if payload["contract_revision"] != 1:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_REVISION",
                    "new EventExperienceContract must begin at revision 1",
                )
            history = connection.execute(
                """
                SELECT contract_id, status
                FROM event_experience_contracts
                WHERE event_seed_id=? AND event_seed_revision=?
                ORDER BY contract_revision DESC LIMIT 1
                """,
                (seed_id, seed_revision),
            ).fetchone()
            if history is not None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_SUPERSESSION_REQUIRED",
                    "an existing contract lineage must be changed through supersede_contract",
                    contract_id=str(history["contract_id"]),
                    status=str(history["status"]),
                )
            contract_hash = canonical_hash(
                payload, exclude_top_level=_CONTRACT_HASH_EXCLUDED
            )
            now = _utc_now()
            try:
                connection.execute(
                    """
                    INSERT INTO event_experience_contracts(
                        contract_id, contract_revision, event_seed_id,
                        event_seed_revision, parent_chain_id, contract_hash,
                        payload_json, status, supersedes_contract_id,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?)
                    """,
                    (
                        payload["contract_id"],
                        payload["contract_revision"],
                        seed_id,
                        seed_revision,
                        payload["parent_chain_id"],
                        contract_hash,
                        canonical_json(payload),
                        payload.get("supersedes_contract_id") or None,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_EXISTS",
                    "contract identity already exists",
                    contract_id=payload["contract_id"],
                ) from exc
            row = connection.execute(
                """
                SELECT * FROM event_experience_contracts
                WHERE contract_id=?
                """,
                (payload["contract_id"],),
            ).fetchone()
            return {"contract": self._contract_from_row(row)}

        return self._mutate(
            operation="propose_contract",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={"contract": raw},
            apply=apply,
        )

    def propose_and_lock_contract(
        self,
        contract: EventExperienceContract | Mapping[str, Any],
        *,
        expected_control_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Atomically persist and lock a high-confidence/delegated contract."""

        raw = _mapping_input(contract, "EventExperienceContract")
        seed_id = _require_text(
            raw.get("event_seed_id"), "event_seed_id", maximum=256
        )
        seed_revision = _require_int(
            raw.get("event_seed_revision"),
            "event_seed_revision",
            minimum=1,
        )

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            seed_row = self._fetch_seed_row(
                connection, seed_id, seed_revision
            )
            if str(seed_row["status"]) != "seeded":
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_NOT_LOCKABLE",
                    "EventSeed must be seeded before atomic contract lock",
                    status=str(seed_row["status"]),
                )
            history = connection.execute(
                """
                SELECT contract_id, status
                FROM event_experience_contracts
                WHERE event_seed_id=? AND event_seed_revision=?
                ORDER BY contract_revision DESC LIMIT 1
                """,
                (seed_id, seed_revision),
            ).fetchone()
            if history is not None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_SUPERSESSION_REQUIRED",
                    "an existing contract lineage must be changed through supersede_contract",
                    contract_id=str(history["contract_id"]),
                    status=str(history["status"]),
                )
            seed_payload = json.loads(str(seed_row["payload_json"]))
            payload = self._contract_payload(raw, seed_payload=seed_payload)
            if payload["contract_revision"] != 1:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_REVISION",
                    "new EventExperienceContract must begin at revision 1",
                )
            self._validate_lockable_contract_payload(payload)
            contract_hash = canonical_hash(
                payload, exclude_top_level=_CONTRACT_HASH_EXCLUDED
            )
            now = _utc_now()
            try:
                connection.execute(
                    """
                    INSERT INTO event_experience_contracts(
                        contract_id, contract_revision, event_seed_id,
                        event_seed_revision, parent_chain_id, contract_hash,
                        payload_json, status, supersedes_contract_id,
                        locked_at, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 'locked', NULL, ?, ?, ?)
                    """,
                    (
                        payload["contract_id"],
                        payload["contract_revision"],
                        seed_id,
                        seed_revision,
                        payload["parent_chain_id"],
                        contract_hash,
                        canonical_json(payload),
                        now,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_EXISTS",
                    "contract identity already exists",
                    contract_id=payload["contract_id"],
                ) from exc
            changed = connection.execute(
                """
                UPDATE event_seeds
                SET status='experience_locked',
                    experience_contract_id=?,
                    experience_contract_hash=?,
                    updated_at=?
                WHERE event_seed_id=? AND event_seed_revision=?
                  AND status='seeded'
                """,
                (
                    payload["contract_id"],
                    contract_hash,
                    now,
                    seed_id,
                    seed_revision,
                ),
            ).rowcount
            if changed != 1:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_LOCK_RACE",
                    "atomic contract lock lost its EventSeed row state",
                )
            row = connection.execute(
                """
                SELECT * FROM event_experience_contracts
                WHERE contract_id=?
                """,
                (payload["contract_id"],),
            ).fetchone()
            return {"contract": self._contract_from_row(row)}

        return self._mutate(
            operation="propose_and_lock_contract",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={"contract": raw},
            apply=apply,
        )

    def lock_contract(
        self,
        contract_id: str,
        *,
        expected_control_revision: int,
        idempotency_key: str,
        expected_contract_hash: str | None = None,
    ) -> dict[str, Any]:
        contract_key = _require_text(
            contract_id, "contract_id", maximum=256
        )
        expected_hash = (
            ""
            if expected_contract_hash is None
            else _require_sha256(
                expected_contract_hash, "expected_contract_hash"
            )
        )

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            row = connection.execute(
                """
                SELECT * FROM event_experience_contracts
                WHERE contract_id=?
                """,
                (contract_key,),
            ).fetchone()
            if row is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_NOT_FOUND",
                    "contract was not found",
                    contract_id=contract_key,
                )
            if str(row["status"]) != "proposed":
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_NOT_PROPOSED",
                    "only a proposed contract can be locked",
                    status=str(row["status"]),
                )
            if expected_hash and str(row["contract_hash"]) != expected_hash:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_HASH_MISMATCH",
                    "contract hash changed before lock",
                )
            payload = json.loads(str(row["payload_json"]))
            self._validate_lockable_contract_payload(payload)
            seed_row = self._fetch_seed_row(
                connection,
                str(row["event_seed_id"]),
                int(row["event_seed_revision"]),
            )
            if str(seed_row["status"]) not in {"seeded", "experience_locked"}:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_NOT_LOCKABLE",
                    "EventSeed is no longer in a contract-lockable state",
                    status=str(seed_row["status"]),
                )
            now = _utc_now()
            changed = connection.execute(
                """
                UPDATE event_experience_contracts
                SET status='locked', locked_at=?, updated_at=?
                WHERE contract_id=? AND status='proposed'
                """,
                (now, now, contract_key),
            ).rowcount
            if changed != 1:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_LOCK_RACE",
                    "contract lock lost its row race",
                )
            connection.execute(
                """
                UPDATE event_seeds
                SET status='experience_locked',
                    experience_contract_id=?,
                    experience_contract_hash=?,
                    updated_at=?
                WHERE event_seed_id=? AND event_seed_revision=?
                """,
                (
                    contract_key,
                    str(row["contract_hash"]),
                    now,
                    str(row["event_seed_id"]),
                    int(row["event_seed_revision"]),
                ),
            )
            locked = connection.execute(
                """
                SELECT * FROM event_experience_contracts
                WHERE contract_id=?
                """,
                (contract_key,),
            ).fetchone()
            return {"contract": self._contract_from_row(locked)}

        return self._mutate(
            operation="lock_contract",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={
                "contract_id": contract_key,
                "expected_contract_hash": expected_hash,
            },
            apply=apply,
        )

    def supersede_contract(
        self,
        contract_id: str,
        replacement: EventExperienceContract | Mapping[str, Any],
        *,
        expected_control_revision: int,
        idempotency_key: str,
        reason: str,
        lock_replacement: bool = False,
    ) -> dict[str, Any]:
        contract_key = _require_text(
            contract_id, "contract_id", maximum=256
        )
        reason_text = _require_text(reason, "reason")
        if not isinstance(lock_replacement, bool):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_BOOLEAN_REQUIRED",
                "lock_replacement must be boolean",
            )
        raw = _mapping_input(replacement, "EventExperienceContract")

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            previous = connection.execute(
                """
                SELECT * FROM event_experience_contracts
                WHERE contract_id=?
                """,
                (contract_key,),
            ).fetchone()
            if previous is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_NOT_FOUND",
                    "contract was not found",
                )
            if str(previous["status"]) == "retired":
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_RETIRED",
                    "retired contract cannot be superseded twice",
                )
            seed_row = self._fetch_seed_row(
                connection,
                str(previous["event_seed_id"]),
                int(previous["event_seed_revision"]),
            )
            if str(seed_row["status"]) in {"expanded", "generated", "retired"}:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_REVISION_REQUIRED",
                    "expanded, generated, or retired events require a new EventSeed revision",
                )
            seed_payload = json.loads(str(seed_row["payload_json"]))
            candidate = dict(raw)
            supplied_supersedes = candidate.get("supersedes_contract_id")
            if supplied_supersedes not in {None, "", contract_key}:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_SUPERSESSION_MISMATCH",
                    "replacement supersedes_contract_id does not match the active contract",
                    expected_contract_id=contract_key,
                    supplied_contract_id=supplied_supersedes,
                )
            expected_revision = int(previous["contract_revision"]) + 1
            if (
                candidate.get("contract_revision") is not None
                and candidate.get("contract_revision") != expected_revision
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_REVISION",
                    "replacement contract_revision must increment exactly once",
                    expected_contract_revision=expected_revision,
                )
            candidate["event_seed_id"] = str(previous["event_seed_id"])
            candidate["event_seed_revision"] = int(previous["event_seed_revision"])
            payload = self._contract_payload(
                candidate,
                seed_payload=seed_payload,
                supersedes_contract_id=contract_key,
                contract_revision=expected_revision,
            )
            if lock_replacement:
                self._validate_lockable_contract_payload(payload)
            if payload["contract_id"] == contract_key:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_ID_REUSED",
                    "a superseding contract requires a new contract_id",
                    contract_id=contract_key,
                )
            contract_hash = canonical_hash(
                payload, exclude_top_level=_CONTRACT_HASH_EXCLUDED
            )
            now = _utc_now()
            connection.execute(
                """
                UPDATE event_experience_contracts
                SET status='retired', retired_at=?, retired_reason=?, updated_at=?
                WHERE contract_id=?
                """,
                (now, reason_text, now, contract_key),
            )
            new_status = "locked" if lock_replacement else "proposed"
            locked_at = now if lock_replacement else None
            try:
                connection.execute(
                    """
                    INSERT INTO event_experience_contracts(
                        contract_id, contract_revision, event_seed_id,
                        event_seed_revision, parent_chain_id, contract_hash,
                        payload_json, status, supersedes_contract_id,
                        locked_at, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["contract_id"],
                        payload["contract_revision"],
                        payload["event_seed_id"],
                        payload["event_seed_revision"],
                        payload["parent_chain_id"],
                        contract_hash,
                        canonical_json(payload),
                        new_status,
                        contract_key,
                        locked_at,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_EXISTS",
                    "replacement contract identity already exists",
                    contract_id=payload["contract_id"],
                ) from exc
            connection.execute(
                """
                UPDATE event_seeds
                SET status=?,
                    experience_contract_id=?,
                    experience_contract_hash=?,
                    updated_at=?
                WHERE event_seed_id=? AND event_seed_revision=?
                """,
                (
                    "experience_locked" if lock_replacement else "seeded",
                    payload["contract_id"] if lock_replacement else None,
                    contract_hash if lock_replacement else None,
                    now,
                    payload["event_seed_id"],
                    payload["event_seed_revision"],
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM event_experience_contracts
                WHERE contract_id=?
                """,
                (payload["contract_id"],),
            ).fetchone()
            return {
                "contract": self._contract_from_row(row),
                "superseded_contract_id": contract_key,
            }

        return self._mutate(
            operation="supersede_contract",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={
                "contract_id": contract_key,
                "replacement": raw,
                "reason": reason_text,
                "lock_replacement": lock_replacement,
            },
            apply=apply,
        )

    def retire_contract(
        self,
        contract_id: str,
        *,
        expected_control_revision: int,
        idempotency_key: str,
        reason: str,
    ) -> dict[str, Any]:
        contract_key = _require_text(
            contract_id, "contract_id", maximum=256
        )
        reason_text = _require_text(reason, "reason")

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            row = connection.execute(
                """
                SELECT * FROM event_experience_contracts
                WHERE contract_id=?
                """,
                (contract_key,),
            ).fetchone()
            if row is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_NOT_FOUND",
                    "contract was not found",
                )
            if str(row["status"]) == "retired":
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_RETIRED",
                    "contract is already retired",
                )
            seed_row = self._fetch_seed_row(
                connection,
                str(row["event_seed_id"]),
                int(row["event_seed_revision"]),
            )
            if str(seed_row["status"]) in {"expanded", "generated"}:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_REVISION_REQUIRED",
                    "expanded or generated events require a new EventSeed revision",
                )
            now = _utc_now()
            connection.execute(
                """
                UPDATE event_experience_contracts
                SET status='retired', retired_at=?, retired_reason=?, updated_at=?
                WHERE contract_id=?
                """,
                (now, reason_text, now, contract_key),
            )
            connection.execute(
                """
                UPDATE event_seeds
                SET status='seeded', experience_contract_id=NULL,
                    experience_contract_hash=NULL, updated_at=?
                WHERE event_seed_id=? AND event_seed_revision=?
                  AND experience_contract_id=?
                """,
                (
                    now,
                    str(row["event_seed_id"]),
                    int(row["event_seed_revision"]),
                    contract_key,
                ),
            )
            retired = connection.execute(
                """
                SELECT * FROM event_experience_contracts
                WHERE contract_id=?
                """,
                (contract_key,),
            ).fetchone()
            return {"contract": self._contract_from_row(retired)}

        return self._mutate(
            operation="retire_contract",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={"contract_id": contract_key, "reason": reason_text},
            apply=apply,
        )

    def get_contract(self, contract_id: str) -> dict[str, Any]:
        contract_key = _require_text(
            contract_id, "contract_id", maximum=256
        )
        with self._transaction(write=False) as connection:
            row = connection.execute(
                """
                SELECT * FROM event_experience_contracts
                WHERE contract_id=?
                """,
                (contract_key,),
            ).fetchone()
            if row is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_NOT_FOUND",
                    "contract was not found",
                )
            return self._contract_from_row(row)

    def active_contract_for_seed(
        self,
        event_seed_id: str,
        event_seed_revision: int,
    ) -> dict[str, Any] | None:
        seed_id = _require_text(
            event_seed_id, "event_seed_id", maximum=256
        )
        seed_revision = _require_int(
            event_seed_revision, "event_seed_revision", minimum=1
        )
        with self._transaction(write=False) as connection:
            row = connection.execute(
                """
                SELECT * FROM event_experience_contracts
                WHERE event_seed_id=? AND event_seed_revision=?
                  AND status IN ('proposed', 'locked')
                ORDER BY contract_revision DESC LIMIT 1
                """,
                (seed_id, seed_revision),
            ).fetchone()
            return None if row is None else self._contract_from_row(row)

    def advance_seed_status(
        self,
        event_seed_id: str,
        event_seed_revision: int,
        target_status: str,
        *,
        expected_control_revision: int,
        idempotency_key: str,
        expected_contract_hash: str,
    ) -> dict[str, Any]:
        """Advance locked EventSeed lifecycle without touching canon state."""

        seed_id = _require_text(
            event_seed_id, "event_seed_id", maximum=256
        )
        seed_revision = _require_int(
            event_seed_revision, "event_seed_revision", minimum=1
        )
        target = _require_text(
            target_status, "target_status", maximum=64
        )
        if target not in {"expanded", "generated"}:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_SEED_STATUS",
                "target_status must be expanded or generated",
                target_status=target,
            )
        contract_hash = _require_sha256(
            expected_contract_hash, "expected_contract_hash"
        )

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            row = self._fetch_seed_row(connection, seed_id, seed_revision)
            current = str(row["status"])
            allowed_transition = {
                "experience_locked": "expanded",
                "expanded": "generated",
            }
            if allowed_transition.get(current) != target:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_TRANSITION",
                    "EventSeed status transition is not allowed",
                    current_status=current,
                    target_status=target,
                )
            if str(row["experience_contract_hash"] or "") != contract_hash:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_HASH_MISMATCH",
                    "EventSeed contract hash changed before status transition",
                )
            contract = connection.execute(
                """
                SELECT contract_hash, status
                FROM event_experience_contracts
                WHERE contract_id=?
                """,
                (row["experience_contract_id"],),
            ).fetchone()
            if (
                contract is None
                or str(contract["status"]) != "locked"
                or str(contract["contract_hash"]) != contract_hash
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_BINDING_INVALID",
                    "EventSeed status transition requires its locked contract",
                )
            now = _utc_now()
            changed = connection.execute(
                """
                UPDATE event_seeds
                SET status=?, updated_at=?
                WHERE event_seed_id=? AND event_seed_revision=?
                  AND status=?
                  AND experience_contract_hash=?
                """,
                (
                    target,
                    now,
                    seed_id,
                    seed_revision,
                    current,
                    contract_hash,
                ),
            ).rowcount
            if changed != 1:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_TRANSITION_RACE",
                    "EventSeed status transition lost its row state",
                )
            advanced = self._fetch_seed_row(
                connection, seed_id, seed_revision
            )
            return {"seed": self._seed_from_row(advanced)}

        return self._mutate(
            operation="advance_seed_status",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={
                "event_seed_id": seed_id,
                "event_seed_revision": seed_revision,
                "target_status": target,
                "expected_contract_hash": contract_hash,
            },
            apply=apply,
        )

    def retire_seed(
        self,
        event_seed_id: str,
        event_seed_revision: int,
        *,
        expected_control_revision: int,
        idempotency_key: str,
        reason: str,
    ) -> dict[str, Any]:
        seed_id = _require_text(
            event_seed_id, "event_seed_id", maximum=256
        )
        seed_revision = _require_int(
            event_seed_revision, "event_seed_revision", minimum=1
        )
        reason_text = _require_text(reason, "reason")

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            row = self._fetch_seed_row(connection, seed_id, seed_revision)
            if str(row["status"]) == "retired":
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_RETIRED",
                    "EventSeed is already retired",
                )
            now = _utc_now()
            connection.execute(
                """
                UPDATE event_seeds
                SET status='retired', retired_at=?, retired_reason=?,
                    updated_at=?
                WHERE event_seed_id=? AND event_seed_revision=?
                """,
                (now, reason_text, now, seed_id, seed_revision),
            )
            connection.execute(
                """
                UPDATE event_experience_contracts
                SET status='retired', retired_at=?, retired_reason=?,
                    updated_at=?
                WHERE event_seed_id=? AND event_seed_revision=?
                  AND status IN ('proposed', 'locked')
                """,
                (now, reason_text, now, seed_id, seed_revision),
            )
            retired = self._fetch_seed_row(connection, seed_id, seed_revision)
            return {"seed": self._seed_from_row(retired)}

        return self._mutate(
            operation="retire_seed",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={
                "event_seed_id": seed_id,
                "event_seed_revision": seed_revision,
                "reason": reason_text,
            },
            apply=apply,
        )

    @staticmethod
    def _normalize_seed_references(
        seed_references: Sequence[Mapping[str, Any] | Sequence[Any]],
    ) -> list[tuple[str, int]]:
        normalized_refs: list[tuple[str, int]] = []
        for index, reference in enumerate(seed_references):
            if isinstance(reference, Mapping):
                seed_id = _require_text(
                    reference.get("event_seed_id"),
                    f"seed_references[{index}].event_seed_id",
                    maximum=256,
                )
                revision = _require_int(
                    reference.get("event_seed_revision"),
                    f"seed_references[{index}].event_seed_revision",
                    minimum=1,
                )
            elif isinstance(reference, Sequence) and not isinstance(
                reference, (str, bytes, bytearray)
            ) and len(reference) == 2:
                seed_id = _require_text(
                    reference[0],
                    f"seed_references[{index}][0]",
                    maximum=256,
                )
                revision = _require_int(
                    reference[1],
                    f"seed_references[{index}][1]",
                    minimum=1,
                )
            else:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_SEED_REFERENCE",
                    "seed reference must contain id and revision",
                )
            normalized_refs.append((seed_id, revision))
        if not normalized_refs:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_MANIFEST_EMPTY",
                "at least one EventSeed is required",
            )
        if len(set(normalized_refs)) != len(normalized_refs):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_MANIFEST_DUPLICATE",
                "manifest EventSeed references must be unique",
            )
        return normalized_refs

    @staticmethod
    def _manifest_context(
        entries: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        fields = (
            "parent_chain_id",
            "branch_id",
            "artifact_id",
            "artifact_revision",
        )
        context: dict[str, Any] = {}
        for field in fields:
            values = {entry[field] for entry in entries}
            if len(values) != 1:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_MANIFEST_CONTEXT_MISMATCH",
                    "manifest EventSeeds must share one chain, branch, and artifact revision",
                    field=field,
                    values=sorted(values, key=lambda value: str(value)),
                )
            context[field] = next(iter(values))
        outline_fields = (
            "source_outline_commit_id",
            "source_outline_artifact_version_id",
            "source_outline_artifact_id",
            "source_outline_artifact_revision",
            "source_outline_content_hash",
        )
        outline_identities = {
            tuple(entry.get(field) for field in outline_fields)
            for entry in entries
        }
        if len(outline_identities) != 1:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_MANIFEST_OUTLINE_MISMATCH",
                "manifest EventSeeds must share one accepted outline binding",
            )
        outline_identity = next(iter(outline_identities))
        if any(outline_identity):
            if not all(outline_identity):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_OUTLINE_BINDING_INCOMPLETE",
                    "manifest accepted outline binding is incomplete",
                )
            context["accepted_outline_binding"] = dict(
                zip(outline_fields, outline_identity)
            )
        return context

    def seed_manifest(
        self,
        seed_references: Sequence[Mapping[str, Any] | Sequence[Any]],
    ) -> dict[str, Any]:
        """Build a stable pre-contract manifest used to bind one question."""

        normalized_refs = self._normalize_seed_references(seed_references)
        with self._transaction(write=False) as connection:
            revision = self._control_revision(connection)
            entries: list[dict[str, Any]] = []
            for seed_id, seed_revision in normalized_refs:
                seed = self._fetch_seed_row(connection, seed_id, seed_revision)
                if str(seed["status"]) == "retired":
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_SEED_RETIRED",
                        "retired EventSeed cannot enter an experience manifest",
                        event_seed_id=seed_id,
                        event_seed_revision=seed_revision,
                    )
                payload = json.loads(str(seed["payload_json"]))
                entry = {
                    "event_seed_id": seed_id,
                    "event_seed_revision": seed_revision,
                    "seed_hash": str(seed["seed_hash"]),
                    "dependency_order": int(seed["dependency_order"]),
                    "parent_chain_id": str(seed["parent_chain_id"]),
                    "branch_id": str(payload.get("branch_id", "main")),
                    "artifact_id": str(payload.get("artifact_id", "")),
                    "artifact_revision": int(
                        payload.get("artifact_revision", 0)
                    ),
                }
                if payload.get("source_outline_commit_id"):
                    entry.update(
                        {
                            "source_outline_commit_id": str(
                                payload["source_outline_commit_id"]
                            ),
                            "source_outline_artifact_version_id": str(
                                payload[
                                    "source_outline_artifact_version_id"
                                ]
                            ),
                            "source_outline_artifact_id": str(
                                payload["source_outline_artifact_id"]
                            ),
                            "source_outline_artifact_revision": int(
                                payload[
                                    "source_outline_artifact_revision"
                                ]
                            ),
                            "source_outline_content_hash": str(
                                payload["source_outline_content_hash"]
                            ),
                        }
                    )
                entries.append(entry)
            entries.sort(
                key=lambda item: (
                    item["dependency_order"],
                    item["event_seed_id"],
                    item["event_seed_revision"],
                )
            )
            context = self._manifest_context(entries)
            manifest_body = {
                "schema_version": EVENT_EXPERIENCE_SCHEMA_VERSION,
                "manifest_kind": "event_seed_candidates",
                **context,
                "seeds": entries,
            }
            return {
                **manifest_body,
                "event_seed_manifest_hash": canonical_hash(manifest_body),
                "control_revision": self._binding_revision(connection),
                "ready": False,
                "blocking_state": "AWAITING_EVENT_EXPERIENCE",
            }

    def _locked_manifest_from_connection(
        self,
        connection: sqlite3.Connection,
        normalized_refs: Sequence[tuple[str, int]],
    ) -> dict[str, Any]:
        """Build a locked manifest inside the caller's existing snapshot."""

        revision = self._control_revision(connection)
        entries: list[dict[str, Any]] = []
        for seed_id, seed_revision in normalized_refs:
            seed = self._fetch_seed_row(connection, seed_id, seed_revision)
            seed_payload = json.loads(str(seed["payload_json"]))
            if str(seed["status"]) not in {
                "experience_locked",
                "expanded",
                "generated",
            }:
                active = connection.execute(
                    """
                    SELECT contract_id, contract_revision, contract_hash,
                           status
                    FROM event_experience_contracts
                    WHERE event_seed_id=? AND event_seed_revision=?
                      AND status IN ('proposed', 'locked')
                    ORDER BY contract_revision DESC LIMIT 1
                    """,
                    (seed_id, seed_revision),
                ).fetchone()
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_REQUIRED",
                    "EventSeed lacks a locked experience contract",
                    event_seed_id=seed_id,
                    event_seed_revision=seed_revision,
                    seed_status=str(seed["status"]),
                    blocking_state="AWAITING_EVENT_EXPERIENCE",
                    active_contract=(
                        None
                        if active is None
                        else {
                            "contract_id": str(active["contract_id"]),
                            "contract_revision": int(
                                active["contract_revision"]
                            ),
                            "contract_hash": str(active["contract_hash"]),
                            "status": str(active["status"]),
                        }
                    ),
                )
            contract = connection.execute(
                """
                SELECT * FROM event_experience_contracts
                WHERE contract_id=? AND status='locked'
                """,
                (seed["experience_contract_id"],),
            ).fetchone()
            if (
                contract is None
                or str(contract["contract_hash"])
                != str(seed["experience_contract_hash"])
                or str(contract["event_seed_id"]) != seed_id
                or int(contract["event_seed_revision"]) != seed_revision
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_BINDING_INVALID",
                    "EventSeed contract binding is missing or stale",
                    event_seed_id=seed_id,
                    event_seed_revision=seed_revision,
                )
            contract_payload = json.loads(str(contract["payload_json"]))
            _validate_supplied_schema(
                contract_payload,
                "locked EventExperienceContract",
            )
            self._validate_lockable_contract_payload(contract_payload)
            entry = {
                    "event_seed_id": seed_id,
                    "event_seed_revision": seed_revision,
                    "seed_hash": str(seed["seed_hash"]),
                    "dependency_order": int(seed["dependency_order"]),
                    "contract_id": str(contract["contract_id"]),
                    "contract_revision": int(contract["contract_revision"]),
                    "contract_hash": str(contract["contract_hash"]),
                    "parent_chain_id": str(seed["parent_chain_id"]),
                    "branch_id": str(
                        contract_payload.get(
                            "branch_id",
                            seed_payload.get("branch_id", "main"),
                        )
                    ),
                    "artifact_id": str(
                        contract_payload.get(
                            "artifact_id",
                            seed_payload.get("artifact_id", ""),
                        )
                    ),
                    "artifact_revision": int(
                        contract_payload.get(
                            "artifact_revision",
                            seed_payload.get("artifact_revision", 0),
                        )
                    ),
                    "source_intent_contract_id": _require_text(
                        contract_payload.get("source_intent_contract_id"),
                        "source_intent_contract_id",
                        maximum=256,
                    ),
                    "source_intent_contract_revision": _require_int(
                        contract_payload.get(
                            "source_intent_contract_revision"
                        ),
                        "source_intent_contract_revision",
                        minimum=1,
                    ),
                    "source_intent_contract_hash": _require_sha256(
                        contract_payload.get("source_intent_contract_hash"),
                        "source_intent_contract_hash",
                    ),
                }
            if seed_payload.get("source_outline_commit_id"):
                entry.update(
                    {
                        "source_outline_commit_id": str(
                            seed_payload["source_outline_commit_id"]
                        ),
                        "source_outline_artifact_version_id": str(
                            seed_payload[
                                "source_outline_artifact_version_id"
                            ]
                        ),
                        "source_outline_artifact_id": str(
                            seed_payload["source_outline_artifact_id"]
                        ),
                        "source_outline_artifact_revision": int(
                            seed_payload[
                                "source_outline_artifact_revision"
                            ]
                        ),
                        "source_outline_content_hash": str(
                            seed_payload["source_outline_content_hash"]
                        ),
                    }
                )
            entries.append(entry)
        entries.sort(
            key=lambda item: (
                item["dependency_order"],
                item["event_seed_id"],
                item["event_seed_revision"],
            )
        )
        context = self._manifest_context(entries)
        intent_identities = {
            (
                item["source_intent_contract_id"],
                item["source_intent_contract_revision"],
                item["source_intent_contract_hash"],
            )
            for item in entries
        }
        if len(intent_identities) != 1:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_MANIFEST_INTENT_MISMATCH",
                "manifest contracts must share one source Intent Contract",
                identities=sorted(
                    intent_identities,
                    key=lambda identity: (identity[0], identity[1]),
                ),
            )
        intent_id, intent_revision, intent_hash = next(iter(intent_identities))
        manifest_body = {
            "schema_version": EVENT_EXPERIENCE_SCHEMA_VERSION,
            "manifest_kind": "locked_event_experience",
            **context,
            "source_intent_contract_id": intent_id,
            "source_intent_contract_revision": intent_revision,
            "source_intent_contract_hash": intent_hash,
            "contracts": entries,
        }
        return {
            **manifest_body,
            "event_seed_manifest_hash": canonical_hash(manifest_body),
            "control_revision": self._binding_revision(connection),
            "ready": True,
            "blocking_state": None,
        }

    def locked_manifest(
        self,
        seed_references: Sequence[Mapping[str, Any] | Sequence[Any]],
    ) -> dict[str, Any]:
        """Build the exact pre-design manifest or fail before remote work."""

        normalized_refs = self._normalize_seed_references(seed_references)
        with self._transaction(write=False) as connection:
            return self._locked_manifest_from_connection(
                connection,
                normalized_refs,
            )

    @staticmethod
    def _validate_manifest_expectations(
        manifest: Mapping[str, Any],
        *,
        expected_event_seed_manifest_hash: str,
        expected_control_revision: int,
    ) -> dict[str, Any]:
        if manifest["control_revision"] != expected_control_revision:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_STALE_CONTROL",
                "event-experience control revision changed",
                expected_control_revision=expected_control_revision,
                actual_control_revision=manifest["control_revision"],
            )
        if (
            manifest["event_seed_manifest_hash"]
            != expected_event_seed_manifest_hash
        ):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_MANIFEST_HASH_MISMATCH",
                "event-experience manifest changed",
                expected_event_seed_manifest_hash=(
                    expected_event_seed_manifest_hash
                ),
                actual_event_seed_manifest_hash=manifest[
                    "event_seed_manifest_hash"
                ],
            )
        return dict(manifest)

    @staticmethod
    def _validate_accepted_outline_binding(
        connection: sqlite3.Connection,
        manifest: Mapping[str, Any],
    ) -> None:
        binding = manifest.get("accepted_outline_binding")
        if not isinstance(binding, Mapping):
            return
        commit_id = _require_text(
            binding.get("source_outline_commit_id"),
            "source_outline_commit_id",
            maximum=256,
        )
        row = connection.execute(
            """
            SELECT
                c.commit_id,
                c.operation,
                p.canon_status AS proposal_status,
                a.artifact_version_id,
                a.artifact_id,
                a.artifact_stage,
                a.canon_status AS artifact_status,
                a.artifact_revision,
                a.content_hash,
                a.content_json,
                a.active
            FROM canon_commits AS c
            JOIN proposals AS p
              ON p.proposal_id=c.proposal_id
            JOIN artifacts AS a
              ON a.artifact_version_id=p.artifact_version_id
            WHERE c.commit_id=?
            """,
            (commit_id,),
        ).fetchone()
        if row is None:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_OUTLINE_BINDING_DRIFT",
                "bound accepted outline commit no longer exists",
                source_outline_commit_id=commit_id,
            )
        expected = {
            "source_outline_artifact_version_id": str(
                binding.get("source_outline_artifact_version_id") or ""
            ),
            "source_outline_artifact_id": str(
                binding.get("source_outline_artifact_id") or ""
            ),
            "source_outline_artifact_revision": int(
                binding.get("source_outline_artifact_revision") or 0
            ),
            "source_outline_content_hash": str(
                binding.get("source_outline_content_hash") or ""
            ),
        }
        actual = {
            "source_outline_artifact_version_id": str(
                row["artifact_version_id"]
            ),
            "source_outline_artifact_id": str(row["artifact_id"]),
            "source_outline_artifact_revision": int(
                row["artifact_revision"]
            ),
            "source_outline_content_hash": str(row["content_hash"]),
        }
        actual["source_outline_content_hash"] = hashlib.sha256(
            str(row["content_json"]).encode("utf-8")
        ).hexdigest()
        lifecycle_valid = (
            str(row["operation"]) == "accept"
            and str(row["proposal_status"]) == "accepted"
            and str(row["artifact_status"]) == "accepted"
            and str(row["artifact_stage"]) == "outline"
            and int(row["active"]) == 1
        )
        mismatches = {
            field: {
                "expected": value,
                "actual": actual[field],
            }
            for field, value in expected.items()
            if actual[field] != value
        }
        if not lifecycle_valid or mismatches:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_OUTLINE_BINDING_DRIFT",
                "bound accepted outline identity is no longer active and exact",
                source_outline_commit_id=commit_id,
                lifecycle_valid=lifecycle_valid,
                mismatches=mismatches,
            )

    def validate_locked_manifest(
        self,
        seed_references: Sequence[Mapping[str, Any] | Sequence[Any]],
        *,
        expected_event_seed_manifest_hash: str,
        expected_control_revision: int,
    ) -> dict[str, Any]:
        """Revalidate the exact zero-remote gate token at lifecycle boundaries."""

        expected_hash = _require_sha256(
            expected_event_seed_manifest_hash,
            "expected_event_seed_manifest_hash",
        )
        expected_revision = self._require_expected_revision(
            expected_control_revision
        )
        normalized_refs = self._normalize_seed_references(seed_references)
        with self._transaction(write=False) as connection:
            manifest = self._locked_manifest_from_connection(
                connection,
                normalized_refs,
            )
            self._validate_accepted_outline_binding(
                connection,
                manifest,
            )
            return self._validate_manifest_expectations(
                manifest,
                expected_event_seed_manifest_hash=expected_hash,
                expected_control_revision=expected_revision,
            )

    def validate_locked_manifest_in_transaction(
        self,
        connection: sqlite3.Connection,
        seed_references: Sequence[Mapping[str, Any] | Sequence[Any]],
        *,
        expected_event_seed_manifest_hash: str,
        expected_control_revision: int,
    ) -> dict[str, Any]:
        """Revalidate the gate token without opening a second DB snapshot."""

        if not isinstance(connection, sqlite3.Connection):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_CONNECTION_REQUIRED",
                "connection must be an sqlite3.Connection",
            )
        if not connection.in_transaction:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_TRANSACTION_REQUIRED",
                "manifest validation must run inside the lifecycle transaction",
            )
        expected_hash = _require_sha256(
            expected_event_seed_manifest_hash,
            "expected_event_seed_manifest_hash",
        )
        expected_revision = self._require_expected_revision(
            expected_control_revision
        )
        normalized_refs = self._normalize_seed_references(seed_references)
        database_rows = connection.execute("PRAGMA database_list").fetchall()
        main_database = next(
            (
                str(row[2])
                for row in database_rows
                if len(row) >= 3 and str(row[1]) == "main"
            ),
            "",
        )
        if (
            not main_database
            or Path(main_database).expanduser().resolve(strict=False)
            != self.database_path
        ):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_CONNECTION_MISMATCH",
                "transaction connection is not bound to the event database",
                expected_database=str(self.database_path),
                actual_database=main_database,
            )
        original_row_factory = connection.row_factory
        try:
            connection.row_factory = sqlite3.Row
            manifest = self._locked_manifest_from_connection(
                connection,
                normalized_refs,
            )
            self._validate_accepted_outline_binding(
                connection,
                manifest,
            )
        finally:
            connection.row_factory = original_row_factory
        return self._validate_manifest_expectations(
            manifest,
            expected_event_seed_manifest_hash=expected_hash,
            expected_control_revision=expected_revision,
        )

    @staticmethod
    def _observed_review_payload(
        value: Any,
        *,
        source_text: str | None,
    ) -> dict[str, Any]:
        raw = _mapping_input(value, "ObservedExperienceReview")
        allowed = {
            "review_id",
            "review_revision",
            "artifact_id",
            "artifact_revision",
            "branch_id",
            "source_commit_id",
            "source_content_hash",
            "assistant_sha256",
            "observed_entry",
            "observed_peak",
            "observed_exit",
            "supporting_quotes",
            "supporting_quote_offsets",
            "drift",
            "severity",
            "recommendation",
            "supersedes_review_id",
        }
        _reject_unknown(raw, allowed, "ObservedExperienceReview")
        if source_text is None:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_REVIEW_SOURCE_REQUIRED",
                "source_text is required to verify supporting quotes",
            )
        source = str(source_text)
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        assistant_hash = _require_sha256(
            raw.get("assistant_sha256"),
            "assistant_sha256",
        )
        if source_hash != assistant_hash:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_ASSISTANT_HASH_MISMATCH",
                "observed source text does not match assistant_sha256",
            )
        quotes = _verbatim_text_list(
            raw.get("supporting_quotes", []),
            "supporting_quotes",
            required=True,
        )
        offsets_raw = raw.get("supporting_quote_offsets", [])
        if not isinstance(offsets_raw, Sequence) or isinstance(
            offsets_raw,
            (str, bytes, bytearray),
        ):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_REVIEW_OFFSETS",
                "supporting_quote_offsets must be an array",
            )
        offsets: list[dict[str, int]] = []
        for index, offset in enumerate(offsets_raw):
            if isinstance(offset, Mapping):
                start = offset.get("start")
                end = offset.get("end")
            elif isinstance(offset, Sequence) and not isinstance(
                offset,
                (str, bytes, bytearray),
            ) and len(offset) == 2:
                start, end = offset
            else:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_OFFSET_SHAPE",
                    "each quote offset must contain start and end",
                    index=index,
                )
            start_int = _require_int(start, f"offset[{index}].start")
            end_int = _require_int(end, f"offset[{index}].end")
            if (
                end_int <= start_int
                or end_int > len(source)
                or source[start_int:end_int] != quotes[index]
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_QUOTE_MISMATCH",
                    "supporting quote is not the exact contiguous source slice",
                    index=index,
                )
            offsets.append({"start": start_int, "end": end_int})
        if len(quotes) != len(offsets):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_REVIEW_EVIDENCE_COUNT",
                "supporting quotes and offsets must have equal length",
            )
        payload = {
            "review_revision": _require_int(
                raw.get("review_revision", 1),
                "review_revision",
                minimum=1,
            ),
            "review_mode": "grandfathered_observed_only",
            "artifact_id": _require_text(
                raw.get("artifact_id"),
                "artifact_id",
                maximum=256,
            ),
            "artifact_revision": _require_int(
                raw.get("artifact_revision"),
                "artifact_revision",
                minimum=1,
            ),
            "branch_id": _require_text(
                raw.get("branch_id", "main"),
                "branch_id",
                maximum=256,
            ),
            "source_commit_id": _require_text(
                raw.get("source_commit_id"),
                "source_commit_id",
                maximum=256,
            ),
            "source_content_hash": _require_sha256(
                raw.get("source_content_hash"),
                "source_content_hash",
            ),
            "assistant_sha256": assistant_hash,
            "observed_entry": _require_text(
                raw.get("observed_entry"),
                "observed_entry",
            ),
            "observed_peak": _require_text(
                raw.get("observed_peak"),
                "observed_peak",
            ),
            "observed_exit": _require_text(
                raw.get("observed_exit"),
                "observed_exit",
            ),
            "supporting_quotes": quotes,
            "supporting_quote_offsets": offsets,
            "drift": _require_text(raw.get("drift"), "drift"),
            "severity": _require_text(raw.get("severity"), "severity"),
            "recommendation": _require_text(
                raw.get("recommendation"),
                "recommendation",
            ),
            "supersedes_review_id": _optional_text(
                raw.get("supersedes_review_id"),
                "supersedes_review_id",
                maximum=256,
            ),
        }
        review_id = _optional_text(
            raw.get("review_id"),
            "review_id",
            maximum=256,
        )
        payload["review_id"] = review_id or _stable_id(
            "observed-experience-review",
            {
                "artifact_id": payload["artifact_id"],
                "artifact_revision": payload["artifact_revision"],
                "branch_id": payload["branch_id"],
                "source_commit_id": payload["source_commit_id"],
                "assistant_sha256": payload["assistant_sha256"],
                "review_revision": payload["review_revision"],
            },
        )
        return payload

    @staticmethod
    def _observed_review_from_row(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(str(row["payload_json"]))
        payload.update(
            {
                "review_hash": str(row["review_hash"]),
                "status": str(row["status"]),
                "retired_at": row["retired_at"],
                "retired_reason": row["retired_reason"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
        )
        return payload

    def record_observed_review(
        self,
        review: Mapping[str, Any],
        *,
        expected_control_revision: int,
        idempotency_key: str,
        source_text: str | None = None,
    ) -> dict[str, Any]:
        payload = self._observed_review_payload(
            review,
            source_text=source_text,
        )
        review_hash = canonical_hash(payload)

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            artifact = connection.execute(
                """
                SELECT
                    c.commit_id,
                    c.operation,
                    p.canon_status AS proposal_status,
                    a.artifact_id,
                    a.branch_id,
                    a.artifact_revision,
                    a.content_hash,
                    a.content_json
                FROM canon_commits AS c
                JOIN proposals AS p
                  ON p.proposal_id=c.proposal_id
                JOIN artifacts AS a
                  ON a.artifact_version_id=p.artifact_version_id
                WHERE c.commit_id=?
                """,
                (payload["source_commit_id"],),
            ).fetchone()
            if artifact is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_OBSERVED_SOURCE_NOT_FOUND",
                    "grandfathered review source commit was not found",
                )
            expected_identity = (
                payload["artifact_id"],
                payload["branch_id"],
                payload["artifact_revision"],
                payload["source_content_hash"],
            )
            actual_identity = (
                str(artifact["artifact_id"]),
                str(artifact["branch_id"]),
                int(artifact["artifact_revision"]),
                hashlib.sha256(
                    str(artifact["content_json"]).encode("utf-8")
                ).hexdigest(),
            )
            if (
                str(artifact["operation"]) != "accept"
                or str(artifact["proposal_status"]) != "accepted"
                or actual_identity != expected_identity
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_OBSERVED_SOURCE_DRIFT",
                    "grandfathered review source identity changed",
                    expected=expected_identity,
                    actual=actual_identity,
                )
            supersedes = payload.get("supersedes_review_id") or ""
            if supersedes:
                previous = connection.execute(
                    """
                    SELECT * FROM event_experience_observed_reviews
                    WHERE review_id=?
                    """,
                    (supersedes,),
                ).fetchone()
                if previous is None or str(previous["status"]) != "recorded":
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_REVIEW_NOT_ACTIVE",
                        "only an active observed review can be superseded",
                    )
                previous_payload = json.loads(str(previous["payload_json"]))
                if (
                    payload["review_revision"]
                    != int(previous["review_revision"]) + 1
                    or any(
                        previous_payload[field] != payload[field]
                        for field in (
                            "artifact_id",
                            "artifact_revision",
                            "branch_id",
                            "source_commit_id",
                            "source_content_hash",
                            "assistant_sha256",
                        )
                    )
                ):
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_REVIEW_LINEAGE_MISMATCH",
                        "observed review supersession changed source identity",
                    )
                connection.execute(
                    """
                    UPDATE event_experience_observed_reviews
                    SET status='superseded', updated_at=?
                    WHERE review_id=? AND status='recorded'
                    """,
                    (_utc_now(), supersedes),
                )
            elif payload["review_revision"] != 1:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_REVISION",
                    "new observed review must begin at revision 1",
                )
            now = _utc_now()
            try:
                connection.execute(
                    """
                    INSERT INTO event_experience_observed_reviews(
                        review_id, review_revision, artifact_id,
                        artifact_revision, branch_id, source_commit_id,
                        source_content_hash, assistant_sha256, review_hash,
                        payload_json, status, supersedes_review_id,
                        created_at, updated_at
                    ) VALUES(
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'recorded', ?, ?, ?
                    )
                    """,
                    (
                        payload["review_id"],
                        payload["review_revision"],
                        payload["artifact_id"],
                        payload["artifact_revision"],
                        payload["branch_id"],
                        payload["source_commit_id"],
                        payload["source_content_hash"],
                        payload["assistant_sha256"],
                        review_hash,
                        _storage_json(payload),
                        supersedes or None,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_EXISTS",
                    "observed review identity already exists",
                    review_id=payload["review_id"],
                ) from exc
            row = connection.execute(
                """
                SELECT * FROM event_experience_observed_reviews
                WHERE review_id=?
                """,
                (payload["review_id"],),
            ).fetchone()
            return {
                "review": self._observed_review_from_row(row),
                "_advance_binding_revision": False,
            }

        return self._mutate(
            operation="record_observed_review",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={"review": payload},
            apply=apply,
        )

    def list_observed_reviews(
        self,
        *,
        artifact_id: str | None = None,
    ) -> list[dict[str, Any]]:
        parameters: tuple[Any, ...] = ()
        where = ""
        if artifact_id is not None:
            where = "WHERE artifact_id=?"
            parameters = (
                _require_text(
                    artifact_id,
                    "artifact_id",
                    maximum=256,
                ),
            )
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM event_experience_observed_reviews
                {where}
                ORDER BY artifact_id, artifact_revision, review_revision,
                         review_id
                """,
                parameters,
            ).fetchall()
            return [
                self._observed_review_from_row(row)
                for row in rows
            ]

    @staticmethod
    def _review_payload(
        value: Any,
        *,
        assistant_text: str | None,
    ) -> dict[str, Any]:
        raw = _mapping_input(value, "ExperienceReview")
        allowed = {
            "review_id",
            "review_revision",
            "proposal_id",
            "receipt_id",
            "assistant_sha256",
            "contract_id",
            "contract_hash",
            "artifact_revision",
            "observed_entry",
            "observed_peak",
            "observed_exit",
            "supporting_quotes",
            "supporting_quote_offsets",
            "drift",
            "severity",
            "recommendation",
            "supersedes_review_id",
        }
        _reject_unknown(raw, allowed, "ExperienceReview")
        quotes = _verbatim_text_list(
            raw.get("supporting_quotes", []),
            "supporting_quotes",
            required=True,
        )
        offsets_raw = raw.get("supporting_quote_offsets", [])
        if not isinstance(offsets_raw, Sequence) or isinstance(
            offsets_raw, (str, bytes, bytearray)
        ):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_REVIEW_OFFSETS",
                "supporting_quote_offsets must be an array",
            )
        offsets: list[dict[str, int]] = []
        for index, offset in enumerate(offsets_raw):
            if isinstance(offset, Mapping):
                start = offset.get("start")
                end = offset.get("end")
            elif isinstance(offset, Sequence) and not isinstance(
                offset, (str, bytes, bytearray)
            ) and len(offset) == 2:
                start, end = offset
            else:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_OFFSET_SHAPE",
                    "each quote offset must contain start and end",
                    index=index,
                )
            start_int = _require_int(start, f"offset[{index}].start")
            end_int = _require_int(end, f"offset[{index}].end")
            if end_int <= start_int:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_OFFSET_RANGE",
                    "quote end must be greater than start",
                    index=index,
                )
            offsets.append({"start": start_int, "end": end_int})
        if len(quotes) != len(offsets):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_REVIEW_EVIDENCE_COUNT",
                "supporting quotes and offsets must have equal length",
            )
        assistant_hash = _require_sha256(
            raw.get("assistant_sha256"), "assistant_sha256"
        )
        if assistant_text is None:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_REVIEW_SOURCE_REQUIRED",
                "assistant_text is required to verify supporting quotes",
            )
        source = str(assistant_text)
        observed_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if observed_hash != assistant_hash:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_ASSISTANT_HASH_MISMATCH",
                "assistant text does not match assistant_sha256",
            )
        for index, (quote, offset) in enumerate(zip(quotes, offsets)):
            if offset["end"] > len(source) or source[
                offset["start"] : offset["end"]
            ] != quote:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_QUOTE_MISMATCH",
                    "supporting quote is not the exact contiguous source slice",
                    index=index,
                )
        payload = {
            "review_revision": _require_int(
                raw.get("review_revision", 1),
                "review_revision",
                minimum=1,
            ),
            "proposal_id": _require_text(
                raw.get("proposal_id"), "proposal_id", maximum=256
            ),
            "receipt_id": _require_text(
                raw.get("receipt_id"), "receipt_id", maximum=256
            ),
            "assistant_sha256": assistant_hash,
            "contract_id": _require_text(
                raw.get("contract_id"), "contract_id", maximum=256
            ),
            "contract_hash": _require_sha256(
                raw.get("contract_hash"), "contract_hash"
            ),
            "artifact_revision": _require_int(
                raw.get("artifact_revision"), "artifact_revision"
            ),
            "observed_entry": _require_text(
                raw.get("observed_entry"), "observed_entry"
            ),
            "observed_peak": _require_text(
                raw.get("observed_peak"), "observed_peak"
            ),
            "observed_exit": _require_text(
                raw.get("observed_exit"), "observed_exit"
            ),
            "supporting_quotes": quotes,
            "supporting_quote_offsets": offsets,
            "drift": _require_text(raw.get("drift"), "drift"),
            "severity": _require_text(raw.get("severity"), "severity"),
            "recommendation": _require_text(
                raw.get("recommendation"), "recommendation"
            ),
            "supersedes_review_id": _optional_text(
                raw.get("supersedes_review_id"),
                "supersedes_review_id",
                maximum=256,
            ),
        }
        review_id = _optional_text(
            raw.get("review_id"), "review_id", maximum=256
        )
        payload["review_id"] = review_id or _stable_id(
            "experience-review",
            {
                "proposal_id": payload["proposal_id"],
                "receipt_id": payload["receipt_id"],
                "contract_id": payload["contract_id"],
                "assistant_sha256": payload["assistant_sha256"],
                "review_revision": payload["review_revision"],
            },
        )
        return payload

    @staticmethod
    def _review_from_row(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(str(row["payload_json"]))
        payload.update(
            {
                "review_hash": str(row["review_hash"]),
                "status": str(row["status"]),
                "retired_at": row["retired_at"],
                "retired_reason": row["retired_reason"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
        )
        return payload

    def record_review(
        self,
        review: ExperienceReview | Mapping[str, Any],
        *,
        expected_control_revision: int,
        idempotency_key: str,
        assistant_text: str | None = None,
    ) -> dict[str, Any]:
        payload = self._review_payload(review, assistant_text=assistant_text)
        review_hash = canonical_hash(payload)

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            contract = connection.execute(
                """
                SELECT contract_hash, payload_json, status, locked_at
                FROM event_experience_contracts
                WHERE contract_id=?
                """,
                (payload["contract_id"],),
            ).fetchone()
            if contract is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_NOT_FOUND",
                    "review contract was not found",
                )
            if str(contract["contract_hash"]) != payload["contract_hash"]:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_CONTRACT_HASH_MISMATCH",
                    "review is bound to a stale contract hash",
                )
            if (
                contract["locked_at"] is None
                or str(contract["status"]) == "proposed"
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_CONTRACT_NOT_LOCKED",
                    "experience review requires a contract that was locked",
                    contract_status=str(contract["status"]),
                )
            contract_payload = json.loads(str(contract["payload_json"]))
            if int(contract_payload.get("artifact_revision", 0)) != int(
                payload["artifact_revision"]
            ):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_ARTIFACT_MISMATCH",
                    "review artifact_revision does not match its contract",
                    contract_artifact_revision=int(
                        contract_payload.get("artifact_revision", 0)
                    ),
                    review_artifact_revision=int(
                        payload["artifact_revision"]
                    ),
                )
            supersedes = payload.get("supersedes_review_id") or ""
            if supersedes:
                previous = connection.execute(
                    """
                    SELECT * FROM event_experience_reviews
                    WHERE review_id=?
                    """,
                    (supersedes,),
                ).fetchone()
                if previous is None:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_REVIEW_NOT_FOUND",
                        "superseded review was not found",
                    )
                if str(previous["status"]) != "recorded":
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_REVIEW_NOT_ACTIVE",
                        "only an active review can be superseded",
                    )
                if str(previous["contract_id"]) != payload["contract_id"]:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_REVIEW_CONTRACT_MISMATCH",
                        "review supersession cannot change contract identity",
                    )
                for field in (
                    "proposal_id",
                    "receipt_id",
                    "assistant_sha256",
                ):
                    if str(previous[field]) != str(payload[field]):
                        raise EventExperienceError(
                            "EVENT_EXPERIENCE_REVIEW_LINEAGE_MISMATCH",
                            f"review supersession cannot change {field}",
                            field=field,
                            previous=str(previous[field]),
                            replacement=str(payload[field]),
                        )
                previous_payload = json.loads(
                    str(previous["payload_json"])
                )
                if int(previous_payload["artifact_revision"]) != int(
                    payload["artifact_revision"]
                ):
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_REVIEW_LINEAGE_MISMATCH",
                        "review supersession cannot change artifact_revision",
                        field="artifact_revision",
                        previous=int(
                            previous_payload["artifact_revision"]
                        ),
                        replacement=int(payload["artifact_revision"]),
                    )
                if payload["review_revision"] != int(previous["review_revision"]) + 1:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_REVIEW_REVISION",
                        "superseding review must increment revision exactly once",
                    )
                if payload["review_id"] == supersedes:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_REVIEW_ID_REUSED",
                        "a superseding review requires a new review_id",
                        review_id=supersedes,
                    )
            elif payload["review_revision"] != 1:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_REVISION",
                    "new review must begin at revision 1",
                )
            now = _utc_now()
            if supersedes:
                connection.execute(
                    """
                    UPDATE event_experience_reviews
                    SET status='superseded', updated_at=?
                    WHERE review_id=?
                    """,
                    (now, supersedes),
                )
            try:
                connection.execute(
                    """
                    INSERT INTO event_experience_reviews(
                        review_id, review_revision, proposal_id, receipt_id,
                        assistant_sha256, contract_id, contract_hash,
                        review_hash, payload_json, status,
                        supersedes_review_id, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'recorded', ?, ?, ?)
                    """,
                    (
                        payload["review_id"],
                        payload["review_revision"],
                        payload["proposal_id"],
                        payload["receipt_id"],
                        payload["assistant_sha256"],
                        payload["contract_id"],
                        payload["contract_hash"],
                        review_hash,
                        _storage_json(payload),
                        supersedes or None,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_EXISTS",
                    "review identity already exists",
                    review_id=payload["review_id"],
                ) from exc
            row = connection.execute(
                """
                SELECT * FROM event_experience_reviews
                WHERE review_id=?
                """,
                (payload["review_id"],),
            ).fetchone()
            return {
                "review": self._review_from_row(row),
                "_advance_binding_revision": False,
            }

        return self._mutate(
            operation="record_review",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={"review": payload},
            apply=apply,
        )

    def get_review(self, review_id: str) -> dict[str, Any]:
        review_key = _require_text(review_id, "review_id", maximum=256)
        with self._transaction(write=False) as connection:
            row = connection.execute(
                """
                SELECT * FROM event_experience_reviews
                WHERE review_id=?
                """,
                (review_key,),
            ).fetchone()
            if row is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_NOT_FOUND",
                    "experience review was not found",
                    review_id=review_key,
                )
            return self._review_from_row(row)

    def list_reviews(
        self,
        *,
        contract_id: str | None = None,
        include_inactive: bool = True,
    ) -> list[dict[str, Any]]:
        if not isinstance(include_inactive, bool):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_BOOLEAN_REQUIRED",
                "include_inactive must be boolean",
            )
        contract_key = (
            ""
            if contract_id is None
            else _require_text(contract_id, "contract_id", maximum=256)
        )
        clauses: list[str] = []
        parameters: list[Any] = []
        if contract_key:
            clauses.append("contract_id=?")
            parameters.append(contract_key)
        if not include_inactive:
            clauses.append("status='recorded'")
        where = "" if not clauses else "WHERE " + " AND ".join(clauses)
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM event_experience_reviews
                {where}
                ORDER BY contract_id, review_revision, review_id
                """,
                tuple(parameters),
            ).fetchall()
            return [self._review_from_row(row) for row in rows]

    def retire_review(
        self,
        review_id: str,
        *,
        expected_control_revision: int,
        idempotency_key: str,
        reason: str,
    ) -> dict[str, Any]:
        review_key = _require_text(review_id, "review_id", maximum=256)
        reason_text = _require_text(reason, "reason")

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            row = connection.execute(
                """
                SELECT * FROM event_experience_reviews
                WHERE review_id=?
                """,
                (review_key,),
            ).fetchone()
            if row is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_NOT_FOUND",
                    "experience review was not found",
                    review_id=review_key,
                )
            if str(row["status"]) != "recorded":
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_REVIEW_NOT_ACTIVE",
                    "only an active experience review can be retired",
                    review_id=review_key,
                    status=str(row["status"]),
                )
            now = _utc_now()
            connection.execute(
                """
                UPDATE event_experience_reviews
                SET status='retired', retired_at=?, retired_reason=?,
                    updated_at=?
                WHERE review_id=? AND status='recorded'
                """,
                (now, reason_text, now, review_key),
            )
            retired = connection.execute(
                """
                SELECT * FROM event_experience_reviews
                WHERE review_id=?
                """,
                (review_key,),
            ).fetchone()
            return {
                "review": self._review_from_row(retired),
                "_advance_binding_revision": False,
            }

        return self._mutate(
            operation="retire_review",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={"review_id": review_key, "reason": reason_text},
            apply=apply,
        )

    @staticmethod
    def _question_payload(
        *,
        event_seed_manifest_hash: str,
        question: str,
        options: Sequence[Mapping[str, Any]],
        recommended_option_id: str,
        rationale: str,
    ) -> dict[str, Any]:
        manifest_hash = _require_sha256(
            event_seed_manifest_hash, "event_seed_manifest_hash"
        )
        if not isinstance(options, Sequence) or isinstance(
            options, (str, bytes, bytearray)
        ):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_QUESTION_OPTIONS",
                "options must be an array",
            )
        normalized_options: list[dict[str, Any]] = []
        for index, option in enumerate(options):
            if not isinstance(option, Mapping):
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_QUESTION_OPTION",
                    "each question option must be an object",
                    index=index,
                )
            unknown = set(option) - {"option_id", "label", "value"}
            if unknown:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_UNKNOWN_FIELD",
                    "question option contains unsupported fields",
                    fields=sorted(unknown),
                )
            normalized_options.append(
                {
                    "option_id": _require_text(
                        option.get("option_id"),
                        f"options[{index}].option_id",
                        maximum=64,
                    ),
                    "label": _require_text(
                        option.get("label"),
                        f"options[{index}].label",
                        maximum=512,
                    ),
                    "value": normalize_for_hash(option.get("value", {})),
                }
            )
        if not 2 <= len(normalized_options) <= 5:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_QUESTION_OPTION_COUNT",
                "event-experience question requires 2 to 5 options",
            )
        option_ids = [item["option_id"] for item in normalized_options]
        folded_option_ids = [item.casefold() for item in option_ids]
        if len(set(folded_option_ids)) != len(folded_option_ids):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_QUESTION_OPTION_DUPLICATE",
                "question option IDs must be unique under case-insensitive selection",
            )
        folded_labels = [
            item["label"].strip().casefold() for item in normalized_options
        ]
        if len(set(folded_labels)) != len(folded_labels):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_QUESTION_LABEL_DUPLICATE",
                "question option labels must be unambiguous",
            )
        recommended = _require_text(
            recommended_option_id, "recommended_option_id", maximum=64
        )
        recommended_matches = [
            option_id
            for option_id in option_ids
            if option_id.casefold() == recommended.casefold()
        ]
        if len(recommended_matches) != 1:
            raise EventExperienceError(
                "EVENT_EXPERIENCE_QUESTION_RECOMMENDATION",
                "recommended option must exist",
            )
        recommended = recommended_matches[0]
        return {
            "schema_version": EVENT_EXPERIENCE_SCHEMA_VERSION,
            "phase": "event_experience",
            "event_seed_manifest_hash": manifest_hash,
            "question": _require_text(question, "question"),
            "options": normalized_options,
            "recommended_option_id": recommended,
            "recommendation_rationale": _require_text(
                rationale, "rationale"
            ),
            "one_question_per_turn": True,
            "suppress_plot_receipt": True,
            "suppress_remote_retrieval": True,
            "suppress_stop_proposal": True,
        }

    @staticmethod
    def _question_from_row(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(str(row["question_json"]))
        status = str(row["status"])
        expired = (
            status
            in {"AWAITING_ANSWER", "AWAITING_EVENT_EXPERIENCE"}
            and _parse_utc(str(row["expires_at"]))
            <= datetime.now(timezone.utc)
        )
        payload.update(
            {
                "question_hash": str(row["question_hash"]),
                "status": status,
                "expired": expired,
                "effective_status": "EXPIRED" if expired else status,
                "invalid_attempts": int(row["invalid_attempts"]),
                "selected_option_id": row["selected_option_id"],
                "selected_at": row["selected_at"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "expires_at": str(row["expires_at"]),
            }
        )
        return payload

    def open_question(
        self,
        *,
        event_seed_manifest_hash: str,
        seed_references: Sequence[Mapping[str, Any] | Sequence[Any]],
        question: str,
        options: Sequence[Mapping[str, Any]],
        recommended_option_id: str,
        rationale: str,
        expected_control_revision: int,
        idempotency_key: str,
        ttl_seconds: int = 21_600,
    ) -> dict[str, Any]:
        supplied_manifest_hash = _require_sha256(
            event_seed_manifest_hash, "event_seed_manifest_hash"
        )
        seed_binding = self.seed_manifest(seed_references)
        if (
            seed_binding["event_seed_manifest_hash"]
            != supplied_manifest_hash
        ):
            raise EventExperienceError(
                "EVENT_EXPERIENCE_QUESTION_MANIFEST_MISMATCH",
                "question manifest hash does not match current EventSeed candidates",
                expected_event_seed_manifest_hash=seed_binding[
                    "event_seed_manifest_hash"
                ],
                supplied_event_seed_manifest_hash=supplied_manifest_hash,
            )
        payload = self._question_payload(
            event_seed_manifest_hash=supplied_manifest_hash,
            question=question,
            options=options,
            recommended_option_id=recommended_option_id,
            rationale=rationale,
        )
        payload["seed_bindings"] = [
            {
                "event_seed_id": str(seed["event_seed_id"]),
                "event_seed_revision": int(seed["event_seed_revision"]),
                "seed_hash": str(seed["seed_hash"]),
            }
            for seed in seed_binding["seeds"]
        ]
        ttl = _require_int(
            ttl_seconds, "ttl_seconds", minimum=60, maximum=604_800
        )
        question_hash = canonical_hash(payload)

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            for seed in seed_binding["seeds"]:
                current = self._fetch_seed_row(
                    connection,
                    str(seed["event_seed_id"]),
                    int(seed["event_seed_revision"]),
                )
                if (
                    str(current["seed_hash"]) != str(seed["seed_hash"])
                    or str(current["status"]) != "seeded"
                ):
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_QUESTION_BINDING_STALE",
                        "question EventSeed binding changed before persistence",
                        event_seed_id=str(seed["event_seed_id"]),
                        event_seed_revision=int(
                            seed["event_seed_revision"]
                        ),
                        seed_status=str(current["status"]),
                    )
                active_contract = connection.execute(
                    """
                    SELECT contract_id, status
                    FROM event_experience_contracts
                    WHERE event_seed_id=? AND event_seed_revision=?
                      AND status IN ('proposed', 'locked')
                    LIMIT 1
                    """,
                    (
                        str(seed["event_seed_id"]),
                        int(seed["event_seed_revision"]),
                    ),
                ).fetchone()
                if active_contract is not None:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_QUESTION_CONTRACT_EXISTS",
                        "question must be opened before a contract candidate is persisted",
                        contract_id=str(active_contract["contract_id"]),
                        contract_status=str(active_contract["status"]),
                    )
            existing = connection.execute(
                """
                SELECT * FROM event_experience_questions
                WHERE event_seed_manifest_hash=?
                """,
                (payload["event_seed_manifest_hash"],),
            ).fetchone()
            if existing is not None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_QUESTION_EXISTS",
                    "manifest already produced its single experience question",
                    status=str(existing["status"]),
                )
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()
            expires_at = (now_dt + timedelta(seconds=ttl)).isoformat()
            connection.execute(
                """
                INSERT INTO event_experience_questions(
                    event_seed_manifest_hash, question_hash, question_json,
                    status, invalid_attempts, created_at, updated_at, expires_at
                ) VALUES(?, ?, ?, 'AWAITING_ANSWER', 0, ?, ?, ?)
                """,
                (
                    payload["event_seed_manifest_hash"],
                    question_hash,
                    canonical_json(payload),
                    now,
                    now,
                    expires_at,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM event_experience_questions
                WHERE event_seed_manifest_hash=?
                """,
                (payload["event_seed_manifest_hash"],),
            ).fetchone()
            return {
                "action": "ask",
                "reason": "structural_ambiguity",
                "question": self._question_from_row(row),
            }

        return self._mutate(
            operation="open_question",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={
                "question": payload,
                "ttl_seconds": ttl,
                "seed_references": seed_binding["seeds"],
            },
            apply=apply,
        )

    @staticmethod
    def _retire_question_candidates(
        connection: sqlite3.Connection,
        question_payload: Mapping[str, Any],
        *,
        retired_at: str,
        reason: str,
    ) -> dict[str, int]:
        bindings = question_payload.get("seed_bindings", [])
        if not isinstance(bindings, list):
            bindings = []
        retired_seeds = 0
        retired_contracts = 0
        bound_pairs: set[tuple[str, int]] = set()
        for binding in bindings:
            if not isinstance(binding, Mapping):
                continue
            seed_id = str(binding.get("event_seed_id", ""))
            try:
                seed_revision = int(binding.get("event_seed_revision"))
            except (TypeError, ValueError):
                continue
            seed_hash = str(binding.get("seed_hash", ""))
            row = connection.execute(
                """
                SELECT seed_hash, status FROM event_seeds
                WHERE event_seed_id=? AND event_seed_revision=?
                """,
                (seed_id, seed_revision),
            ).fetchone()
            if (
                row is None
                or str(row["seed_hash"]) != seed_hash
                or str(row["status"]) != "seeded"
            ):
                continue
            retired_contracts += connection.execute(
                """
                UPDATE event_experience_contracts
                SET status='retired', retired_at=?, retired_reason=?,
                    updated_at=?
                WHERE event_seed_id=? AND event_seed_revision=?
                  AND status='proposed'
                """,
                (
                    retired_at,
                    reason,
                    retired_at,
                    seed_id,
                    seed_revision,
                ),
            ).rowcount
            retired_seeds += connection.execute(
                """
                UPDATE event_seeds
                SET status='retired', retired_at=?, retired_reason=?,
                    updated_at=?
                WHERE event_seed_id=? AND event_seed_revision=?
                  AND status='seeded' AND seed_hash=?
                """,
                (
                    retired_at,
                    reason,
                    retired_at,
                    seed_id,
                    seed_revision,
                    seed_hash,
                ),
            ).rowcount
            bound_pairs.add((seed_id, seed_revision))

        retired_arcs = 0
        if bound_pairs:
            arcs = connection.execute(
                """
                SELECT arc_id, arc_revision, payload_json
                FROM event_experience_arcs
                WHERE status='proposed'
                """
            ).fetchall()
            for arc in arcs:
                arc_payload = json.loads(str(arc["payload_json"]))
                arc_pairs = {
                    (
                        str(item.get("event_seed_id", "")),
                        int(item.get("event_seed_revision", -1)),
                    )
                    for item in arc_payload.get("event_seed_bindings", [])
                    if isinstance(item, Mapping)
                }
                if not (arc_pairs & bound_pairs):
                    continue
                retired_arcs += connection.execute(
                    """
                    UPDATE event_experience_arcs
                    SET status='retired', retired_at=?, retired_reason=?,
                        updated_at=?
                    WHERE arc_id=? AND arc_revision=? AND status='proposed'
                    """,
                    (
                        retired_at,
                        reason,
                        retired_at,
                        str(arc["arc_id"]),
                        int(arc["arc_revision"]),
                    ),
                ).rowcount
        return {
            "retired_seed_count": retired_seeds,
            "retired_arc_count": retired_arcs,
            "retired_contract_count": retired_contracts,
        }

    def get_question(self, event_seed_manifest_hash: str) -> dict[str, Any]:
        manifest_hash = _require_sha256(
            event_seed_manifest_hash, "event_seed_manifest_hash"
        )
        with self._transaction(write=False) as connection:
            row = connection.execute(
                """
                SELECT * FROM event_experience_questions
                WHERE event_seed_manifest_hash=?
                """,
                (manifest_hash,),
            ).fetchone()
            if row is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_QUESTION_NOT_FOUND",
                    "event-experience question was not found",
                )
            return self._question_from_row(row)

    @staticmethod
    def _select_question_option(
        payload: Mapping[str, Any],
        answer: str,
    ) -> tuple[dict[str, Any] | None, str]:
        compact = _normal_string(answer).strip()
        if compact in _RECOMMENDED_ANSWERS:
            option_id = str(payload["recommended_option_id"])
            source = "recommended_delegation"
        else:
            option_id = compact
            source = "explicit_choice"
        options = list(payload["options"])
        by_id = {str(item["option_id"]).casefold(): item for item in options}
        if option_id.casefold() in by_id:
            return dict(by_id[option_id.casefold()]), source
        if len(compact) == 1 and compact.upper() in "ABCDE":
            index = ord(compact.upper()) - ord("A")
            if index < len(options):
                return dict(options[index]), source
        exact_labels = [
            item
            for item in options
            if str(item["label"]).strip().casefold() == compact.casefold()
        ]
        if len(exact_labels) == 1:
            return dict(exact_labels[0]), source
        return None, ""

    def answer_question(
        self,
        event_seed_manifest_hash: str,
        answer: str,
        *,
        expected_control_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Answer the one bounded question.

        Invalid attempts update only bounded interaction state and intentionally
        do not advance the semantic control revision.  The first invalid answer
        repeats the exact question; the second enters a stable waiting state.
        """

        manifest_hash = _require_sha256(
            event_seed_manifest_hash, "event_seed_manifest_hash"
        )
        answer_text = _normal_string(str(answer or "")).strip()
        expected = self._require_expected_revision(expected_control_revision)
        key = self._require_idempotency_key(idempotency_key)
        request = {
            "event_seed_manifest_hash": manifest_hash,
            "answer": answer_text,
        }
        request_hash = canonical_hash(request)
        operation = "answer_question"
        with self._transaction(write=True) as connection:
            previous_response = connection.execute(
                """
                SELECT request_hash, response_json
                FROM event_experience_idempotency
                WHERE operation=? AND idempotency_key=?
                """,
                (operation, key),
            ).fetchone()
            if previous_response is not None:
                if str(previous_response["request_hash"]) != request_hash:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_IDEMPOTENCY_CONFLICT",
                        "idempotency key was already used with a different answer",
                    )
                return json.loads(str(previous_response["response_json"]))
            actual = self._control_revision(connection)
            if actual != expected:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_STALE_CONTROL",
                    "event-experience control revision changed",
                    expected_control_revision=expected,
                    actual_control_revision=actual,
                )
            row = connection.execute(
                """
                SELECT * FROM event_experience_questions
                WHERE event_seed_manifest_hash=?
                """,
                (manifest_hash,),
            ).fetchone()
            if row is None:
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_QUESTION_NOT_FOUND",
                    "event-experience question was not found",
                )
            payload = json.loads(str(row["question_json"]))
            status = str(row["status"])
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()
            if status == "RETIRED":
                raise EventExperienceError(
                    "EVENT_EXPERIENCE_QUESTION_RETIRED",
                    "event-experience question is retired",
                )
            if (
                status
                in {"AWAITING_ANSWER", "AWAITING_EVENT_EXPERIENCE"}
                and _parse_utc(str(row["expires_at"])) <= now_dt
            ):
                connection.execute(
                    """
                    UPDATE event_experience_questions
                    SET status='RETIRED', updated_at=?
                    WHERE event_seed_manifest_hash=?
                      AND status IN (
                        'AWAITING_ANSWER',
                        'AWAITING_EVENT_EXPERIENCE'
                      )
                    """,
                    (now, manifest_hash),
                )
                next_revision = actual + 1
                changed = connection.execute(
                    """
                    UPDATE event_experience_meta SET value=?
                    WHERE key='control_revision' AND value=?
                    """,
                    (str(next_revision), str(actual)),
                ).rowcount
                if changed != 1:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_STALE_CONTROL",
                        "question expiry lost its control CAS",
                    )
                updated = connection.execute(
                    """
                    SELECT * FROM event_experience_questions
                    WHERE event_seed_manifest_hash=?
                    """,
                    (manifest_hash,),
                ).fetchone()
                retired_candidates = self._retire_question_candidates(
                    connection,
                    payload,
                    retired_at=now,
                    reason="event experience question TTL expired",
                )
                response = {
                    "action": "expired",
                    "reason": "session_ttl_expired",
                    "question": self._question_from_row(updated),
                    "control_revision": next_revision,
                    **retired_candidates,
                }
            elif status == "ANSWERED":
                selected = next(
                    item
                    for item in payload["options"]
                    if item["option_id"] == row["selected_option_id"]
                )
                requested, _ = self._select_question_option(
                    payload, answer_text
                )
                if (
                    requested is not None
                    and requested["option_id"] != selected["option_id"]
                ):
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_QUESTION_ALREADY_ANSWERED",
                        "question is already bound to a different option",
                        selected_option_id=selected["option_id"],
                        requested_option_id=requested["option_id"],
                    )
                response = {
                    "action": "selected",
                    "reason": "already_answered",
                    "selected_option": selected,
                    "question": self._question_from_row(row),
                    "control_revision": actual,
                }
            elif answer_text in _CANCEL_ANSWERS:
                changed_question = connection.execute(
                    """
                    UPDATE event_experience_questions
                    SET status='RETIRED', updated_at=?
                    WHERE event_seed_manifest_hash=?
                      AND status IN (
                        'AWAITING_ANSWER',
                        'AWAITING_EVENT_EXPERIENCE'
                      )
                    """,
                    (now, manifest_hash),
                ).rowcount
                if changed_question != 1:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_QUESTION_STATE_RACE",
                        "question cancel lost its row state",
                    )
                next_revision = actual + 1
                changed_revision = connection.execute(
                    """
                    UPDATE event_experience_meta SET value=?
                    WHERE key='control_revision' AND value=?
                    """,
                    (str(next_revision), str(actual)),
                ).rowcount
                if changed_revision != 1:
                    raise EventExperienceError(
                        "EVENT_EXPERIENCE_STALE_CONTROL",
                        "question cancel lost its control CAS",
                    )
                updated = connection.execute(
                    """
                    SELECT * FROM event_experience_questions
                    WHERE event_seed_manifest_hash=?
                    """,
                    (manifest_hash,),
                ).fetchone()
                retired_candidates = self._retire_question_candidates(
                    connection,
                    payload,
                    retired_at=now,
                    reason="event experience question explicitly cancelled",
                )
                response = {
                    "action": "cancelled",
                    "reason": "explicit_cancel",
                    "question": self._question_from_row(updated),
                    "control_revision": next_revision,
                    **retired_candidates,
                }
            else:
                selected, source = self._select_question_option(
                    payload, answer_text
                )
                if selected is None:
                    prior_attempts = int(row["invalid_attempts"])
                    attempts = min(2, prior_attempts + 1)
                    next_status = (
                        "AWAITING_ANSWER"
                        if attempts == 1
                        else "AWAITING_EVENT_EXPERIENCE"
                    )
                    if prior_attempts < 2:
                        connection.execute(
                            """
                            UPDATE event_experience_questions
                            SET invalid_attempts=?, status=?, updated_at=?
                            WHERE event_seed_manifest_hash=?
                            """,
                            (attempts, next_status, now, manifest_hash),
                        )
                        updated = connection.execute(
                            """
                            SELECT * FROM event_experience_questions
                            WHERE event_seed_manifest_hash=?
                            """,
                            (manifest_hash,),
                        ).fetchone()
                    else:
                        updated = row
                    response = {
                        "action": (
                            "repeat"
                            if attempts == 1
                            else "awaiting_explicit_choice"
                        ),
                        "reason": (
                            "invalid_answer_repeated_once"
                            if attempts == 1
                            else "bounded_question_limit_reached"
                        ),
                        "question": self._question_from_row(updated),
                        "control_revision": actual,
                    }
                else:
                    changed_question = connection.execute(
                        """
                        UPDATE event_experience_questions
                        SET status='ANSWERED', selected_option_id=?,
                            selected_at=?, updated_at=?
                        WHERE event_seed_manifest_hash=?
                          AND status IN (
                            'AWAITING_ANSWER',
                            'AWAITING_EVENT_EXPERIENCE'
                          )
                        """,
                        (selected["option_id"], now, now, manifest_hash),
                    ).rowcount
                    if changed_question != 1:
                        raise EventExperienceError(
                            "EVENT_EXPERIENCE_QUESTION_STATE_RACE",
                            "question selection lost its row state",
                        )
                    next_revision = actual + 1
                    changed = connection.execute(
                        """
                        UPDATE event_experience_meta SET value=?
                        WHERE key='control_revision' AND value=?
                        """,
                        (str(next_revision), str(actual)),
                    ).rowcount
                    if changed != 1:
                        raise EventExperienceError(
                            "EVENT_EXPERIENCE_STALE_CONTROL",
                            "question answer lost its control CAS",
                        )
                    updated = connection.execute(
                        """
                        SELECT * FROM event_experience_questions
                        WHERE event_seed_manifest_hash=?
                        """,
                        (manifest_hash,),
                    ).fetchone()
                    response = {
                        "action": "selected",
                        "reason": source,
                        "selected_option": selected,
                        "question": self._question_from_row(updated),
                        "control_revision": next_revision,
                    }
            connection.execute(
                """
                INSERT INTO event_experience_idempotency(
                    operation, idempotency_key, request_hash,
                    response_json, created_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (operation, key, request_hash, _storage_json(response), now),
            )
            return response

    def retire_expired_questions(
        self,
        *,
        expected_control_revision: int,
        idempotency_key: str,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        observed = (
            datetime.now(timezone.utc)
            if observed_at is None
            else _parse_utc(observed_at)
        )
        observed_text = observed.isoformat()
        request_observed_at = (
            "__NOW__" if observed_at is None else observed_text
        )

        def apply(connection: sqlite3.Connection, _: int) -> dict[str, Any]:
            rows = connection.execute(
                """
                SELECT event_seed_manifest_hash, question_json
                FROM event_experience_questions
                WHERE status IN ('AWAITING_ANSWER', 'AWAITING_EVENT_EXPERIENCE')
                  AND expires_at<=?
                ORDER BY event_seed_manifest_hash
                """,
                (observed_text,),
            ).fetchall()
            manifests = [
                str(row["event_seed_manifest_hash"]) for row in rows
            ]
            retired_seed_count = 0
            retired_arc_count = 0
            retired_contract_count = 0
            if manifests:
                placeholders = ",".join("?" for _ in manifests)
                connection.execute(
                    f"""
                    UPDATE event_experience_questions
                    SET status='RETIRED', updated_at=?
                    WHERE event_seed_manifest_hash IN ({placeholders})
                    """,
                    (observed_text, *manifests),
                )
                for row in rows:
                    candidate_counts = self._retire_question_candidates(
                        connection,
                        json.loads(str(row["question_json"])),
                        retired_at=observed_text,
                        reason="event experience question TTL expired",
                    )
                    retired_seed_count += candidate_counts[
                        "retired_seed_count"
                    ]
                    retired_arc_count += candidate_counts[
                        "retired_arc_count"
                    ]
                    retired_contract_count += candidate_counts[
                        "retired_contract_count"
                    ]
            return {
                "retired_count": len(manifests),
                "event_seed_manifest_hashes": manifests,
                "retired_seed_count": retired_seed_count,
                "retired_arc_count": retired_arc_count,
                "retired_contract_count": retired_contract_count,
                "observed_at": observed_text,
                "_advance_control_revision": bool(manifests),
            }

        return self._mutate(
            operation="retire_expired_questions",
            idempotency_key=idempotency_key,
            expected_control_revision=expected_control_revision,
            request={"observed_at": request_observed_at},
            apply=apply,
        )

    def storage_boundary_report(self) -> dict[str, Any]:
        """Return inspectable schema evidence for the non-canonical boundary."""

        with self._transaction(write=False) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                    """
                )
            }
            triggers = [
                {
                    "name": str(row["name"]),
                    "table": str(row["tbl_name"]),
                    "sql": str(row["sql"] or ""),
                }
                for row in connection.execute(
                    """
                    SELECT name, tbl_name, sql FROM sqlite_master
                    WHERE type='trigger'
                      AND tbl_name IN (
                        'event_seeds',
                        'event_experience_arcs',
                        'event_experience_contracts',
                        'event_experience_reviews',
                        'event_experience_observed_reviews',
                        'event_experience_questions'
                      )
                    ORDER BY name
                    """
                )
            ]
            missing_tables = sorted(CONTROL_TABLES - tables)
            row_counts = {
                table: int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0]
                )
                for table in sorted(tables & CONTROL_TABLES)
            }
            foreign_keys: list[dict[str, Any]] = []
            foreign_key_violations: list[dict[str, Any]] = []
            for table in sorted(tables & CONTROL_TABLES):
                for row in connection.execute(
                    f'PRAGMA foreign_key_list("{table}")'
                ):
                    evidence = {
                        "from_table": table,
                        "from_column": str(row["from"]),
                        "to_table": str(row["table"]),
                        "to_column": str(row["to"]),
                    }
                    foreign_keys.append(evidence)
                    if evidence["to_table"] not in CONTROL_TABLES:
                        foreign_key_violations.append(evidence)
            violations: list[dict[str, Any]] = []
            violations.extend(
                {
                    "kind": "control_trigger",
                    "name": trigger["name"],
                    "table": trigger["table"],
                }
                for trigger in triggers
            )
            violations.extend(
                {"kind": "foreign_key_escape", **item}
                for item in foreign_key_violations
            )
            violations.extend(
                {"kind": "missing_control_table", "table": table}
                for table in missing_tables
            )
            return {
                "schema_version": EVENT_EXPERIENCE_DATABASE_SCHEMA_VERSION,
                "hash_protocol": self._hash_protocol(connection),
                "control_revision": self._control_revision(connection),
                "binding_revision": self._binding_revision(connection),
                "control_tables": sorted(tables & CONTROL_TABLES),
                "foreign_tables": sorted(tables - CONTROL_TABLES),
                "missing_control_tables": missing_tables,
                "control_row_counts": row_counts,
                "control_triggers": triggers,
                "control_foreign_keys": foreign_keys,
                "foreign_key_violations": foreign_key_violations,
                "boundary_violations": violations,
                "boundary_ok": not violations,
                "write_scope": sorted(CONTROL_TABLES),
                "external_exclusion_verification_required": [
                    "continuity replay membership",
                    "authority source manifest",
                    "FTS and vector indexes",
                    "chapter and arc summaries",
                    "working episodic semantic memory",
                ],
            }


__all__ = [
    "EVENT_EXPERIENCE_SCHEMA_VERSION",
    "EVENT_EXPERIENCE_DATABASE_SCHEMA_VERSION",
    "EVENT_EXPERIENCE_HASH_PROTOCOL",
    "CONTROL_TABLES",
    "EventExperienceError",
    "EventSeed",
    "EventExperienceArc",
    "EventExperienceContract",
    "ExperienceReview",
    "EventExperienceService",
    "normalize_for_hash",
    "canonical_json",
    "canonical_hash",
]
