"""Deterministic projection for special items and protagonist advantages.

The Advantage projection is deliberately independent from the established
continuity and item projection hashes.  It models the stable definition,
anchor, modules, runtime, causal ledger, knowledge planes, in-world contracts
and reader-facing narrative contract of a "golden finger" without treating
model-supplied before/after state as authority.

The module owns an additive SQLite surface.  ``ensure_advantage_schema`` is
safe to call on both a full continuity database and an isolated in-memory
database, which keeps shadow validation, replay and read-only query adapters
on the same deterministic implementation.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def ContinuityError(*args: Any, **kwargs: Any) -> Exception:
    """Lazily construct the shared error without a schema import cycle."""

    from .validators import ContinuityError as SharedContinuityError

    return SharedContinuityError(*args, **kwargs)

ADVANTAGE_SCHEMA_VERSION = "plot-rag-advantage/v1"
ADVANTAGE_PROJECTION_SCHEMA_VERSION = 1

ADVANTAGE_META_VERSION = "schema_version"
ADVANTAGE_META_HASH = "projection_hash"
ADVANTAGE_META_HEAD_REVISION = "source_head_revision"
ADVANTAGE_META_ACTIVE_REVISION = "source_active_revision"

ADVANTAGE_STATUSES = frozenset({"canon", "planned", "rumor", "misread"})
ADVANTAGE_ACTIVE_STATUSES = frozenset({"active", "deprecated", "superseded"})
ADVANTAGE_VISIBLE_LIFECYCLE_STATUSES = frozenset({"active"})
ADVANTAGE_ANCHOR_TYPES = frozenset(
    {
        "item_instance",
        "item_stack",
        "body_or_vessel",
        "actor",
        "virtual_system",
        "knowledge_set",
        "temporal_rule",
        "contract",
        "location",
        "power_source",
        "social_graph",
    }
)
ADVANTAGE_KNOWLEDGE_PLANES = frozenset(
    {
        "objective",
        "actor_belief",
        "public_narrative",
        "reader_disclosed",
        "author_plan",
    }
)
ADVANTAGE_GENERATION_KNOWLEDGE_PLANES = frozenset(
    {"public_narrative", "reader_disclosed"}
)
ADVANTAGE_VISIBLE_MODULE_STATUSES = frozenset({"available", "enabled"})
ADVANTAGE_VISIBLE_SLOT_STATUSES = frozenset({"available", "filled"})
ADVANTAGE_DEFAULT_VISIBLE_REVEAL_STAGES = frozenset(
    {
        "initial",
        "current",
        "canon",
        "public",
        "reader_disclosed",
        "revealed",
    }
)
ADVANTAGE_HIDDEN_REVEAL_STAGES = frozenset(
    {
        "author_known",
        "future",
        "hidden",
        "planned",
        "deferred",
        "unknown",
    }
)
ADVANTAGE_EVENT_TYPES = frozenset(
    {
        "advantage_spec",
        "advantage_anchor",
        "advantage_module",
        "advantage_bind",
        "advantage_activate",
        "advantage_trigger",
        "advantage_use",
        "advantage_reward",
        "advantage_cost",
        "advantage_upgrade",
        "advantage_reveal",
        "advantage_contract",
        "advantage_correction",
    }
)
ADVANTAGE_BRANCH_LOCAL_EVENT_TYPES = frozenset(
    {
        "advantage_bind",
        "advantage_activate",
        "advantage_trigger",
        "advantage_use",
        "advantage_reward",
        "advantage_cost",
        "advantage_upgrade",
        "advantage_correction",
    }
)
ADVANTAGE_LEDGER_KINDS = frozenset(
    {
        "bind",
        "activate",
        "trigger",
        "use",
        "reward",
        "cost",
        "upgrade",
        "reveal",
        "contract",
        "breach",
        "correction",
        "bootstrap",
    }
)

ADVANTAGE_PROJECTION_TABLES = (
    "advantage_definitions",
    "advantage_anchors",
    "advantage_module_definitions",
    "advantage_runtime_slots",
    "advantage_runtime_state",
    "advantage_ledger",
    "advantage_knowledge",
    "advantage_contracts",
    "advantage_narrative_contracts",
)

ADVANTAGE_PROJECTION_INDEXES = frozenset(
    {
        "idx_advantage_definitions_status",
        "idx_advantage_anchors_advantage",
        "idx_advantage_anchors_owner",
        "idx_advantage_anchors_ref",
        "idx_advantage_modules_advantage",
        "idx_advantage_slots_advantage",
        "idx_advantage_runtime_owner",
        "idx_advantage_ledger_advantage",
        "idx_advantage_ledger_module",
        "idx_advantage_knowledge_scope",
        "idx_advantage_contracts_advantage",
        "idx_advantage_narrative_status",
    }
)

_ADVANTAGE_TABLE_DELETE_ORDER = (
    "advantage_projection_meta",
    "advantage_ledger",
    "advantage_knowledge",
    "advantage_contracts",
    "advantage_narrative_contracts",
    "advantage_runtime_state",
    "advantage_runtime_slots",
    "advantage_module_definitions",
    "advantage_anchors",
    "advantage_definitions",
)

_JSON_COLUMNS = frozenset(
    {
        "profiles_json",
        "promise_json",
        "counterplay_json",
        "definition_json",
        "source_claim_ids_json",
        "transfer_rule_json",
        "attributes_json",
        "story_coordinate_json",
        "trigger_json",
        "preconditions_json",
        "targets_json",
        "costs_json",
        "effects_json",
        "side_effects_json",
        "failure_modes_json",
        "counters_json",
        "experience_contract_json",
        "unlock_graph_json",
        "set_membership_json",
        "cooldown_until_json",
        "resources_json",
        "unlocked_modules_json",
        "runtime_json",
        "input_json",
        "output_json",
        "loss_json",
        "provenance_json",
        "claim_json",
        "evidence_json",
        "terms_json",
        "agency_json",
        "breach_effect_json",
        "reading_promise_json",
        "reward_loop_json",
        "risk_loop_json",
        "reveal_ladder_json",
        "experience_binding_json",
        "value_json",
    }
)

_EPSILON = 1e-9
_GENERATION_REDACTED = object()

_ADVANTAGE_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "advantage_anchors": {
        "authority_status": (
            "TEXT NOT NULL DEFAULT 'canon' "
            "CHECK(authority_status IN "
            "('canon', 'planned', 'rumor', 'misread'))"
        ),
    },
    "advantage_module_definitions": {
        "authority_status": (
            "TEXT NOT NULL DEFAULT 'canon' "
            "CHECK(authority_status IN "
            "('canon', 'planned', 'rumor', 'misread'))"
        ),
        "experience_contract_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "advantage_runtime_slots": {
        "authority_status": (
            "TEXT NOT NULL DEFAULT 'canon' "
            "CHECK(authority_status IN "
            "('canon', 'planned', 'rumor', 'misread'))"
        ),
    },
    "advantage_contracts": {
        "authority_status": (
            "TEXT NOT NULL DEFAULT 'canon' "
            "CHECK(authority_status IN "
            "('canon', 'planned', 'rumor', 'misread'))"
        ),
    },
    "advantage_narrative_contracts": {
        "authority_status": (
            "TEXT NOT NULL DEFAULT 'canon' "
            "CHECK(authority_status IN "
            "('canon', 'planned', 'rumor', 'misread'))"
        ),
    },
}


ADVANTAGE_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS advantage_definitions (
    advantage_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    profiles_json TEXT NOT NULL DEFAULT '[]',
    anchor_type TEXT NOT NULL,
    acquisition_mode TEXT NOT NULL,
    uniqueness TEXT NOT NULL,
    advantage_status TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL,
    promise_json TEXT NOT NULL DEFAULT '{}',
    counterplay_json TEXT NOT NULL DEFAULT '[]',
    definition_json TEXT NOT NULL DEFAULT '{}',
    source_claim_ids_json TEXT NOT NULL DEFAULT '[]',
    source_event_id TEXT,
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(
        advantage_status IN ('canon', 'planned', 'rumor', 'misread')
    ),
    CHECK(
        lifecycle_status IN ('active', 'deprecated', 'superseded')
    ),
    CHECK(
        anchor_type IN (
            'item_instance', 'item_stack', 'body_or_vessel', 'actor',
            'virtual_system', 'knowledge_set', 'temporal_rule', 'contract',
            'location', 'power_source', 'social_graph'
        )
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0)
);

CREATE INDEX IF NOT EXISTS idx_advantage_definitions_status
    ON advantage_definitions(
        advantage_status, lifecycle_status, advantage_id
    );

CREATE TABLE IF NOT EXISTS advantage_anchors (
    anchor_id TEXT PRIMARY KEY,
    advantage_id TEXT NOT NULL,
    anchor_type TEXT NOT NULL,
    anchor_ref_id TEXT NOT NULL,
    owner_entity_id TEXT,
    binding_state TEXT NOT NULL,
    transfer_rule_json TEXT NOT NULL DEFAULT '{}',
    authority_status TEXT NOT NULL DEFAULT 'canon',
    anchor_status TEXT NOT NULL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    source_claim_ids_json TEXT NOT NULL DEFAULT '[]',
    source_event_id TEXT,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(
        anchor_type IN (
            'item_instance', 'item_stack', 'body_or_vessel', 'actor',
            'virtual_system', 'knowledge_set', 'temporal_rule', 'contract',
            'location', 'power_source', 'social_graph'
        )
    ),
    CHECK(
        binding_state IN (
            'unbound', 'bound', 'dormant', 'sealed', 'contested', 'released'
        )
    ),
    CHECK(authority_status IN ('canon', 'planned', 'rumor', 'misread')),
    CHECK(anchor_status IN ('active', 'deprecated', 'superseded')),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(advantage_id)
        REFERENCES advantage_definitions(advantage_id)
);

CREATE INDEX IF NOT EXISTS idx_advantage_anchors_advantage
    ON advantage_anchors(
        advantage_id, anchor_status, binding_state, anchor_id
    );
CREATE INDEX IF NOT EXISTS idx_advantage_anchors_owner
    ON advantage_anchors(owner_entity_id, binding_state, anchor_id);
CREATE INDEX IF NOT EXISTS idx_advantage_anchors_ref
    ON advantage_anchors(anchor_type, anchor_ref_id, anchor_status);

CREATE TABLE IF NOT EXISTS advantage_module_definitions (
    module_id TEXT PRIMARY KEY,
    advantage_id TEXT NOT NULL,
    title TEXT NOT NULL,
    module_kind TEXT NOT NULL,
    authority_status TEXT NOT NULL DEFAULT 'canon',
    module_status TEXT NOT NULL,
    stage TEXT NOT NULL,
    experience_contract_json TEXT NOT NULL DEFAULT '{}',
    trigger_json TEXT NOT NULL DEFAULT '{}',
    preconditions_json TEXT NOT NULL DEFAULT '[]',
    targets_json TEXT NOT NULL DEFAULT '[]',
    costs_json TEXT NOT NULL DEFAULT '[]',
    effects_json TEXT NOT NULL DEFAULT '[]',
    side_effects_json TEXT NOT NULL DEFAULT '[]',
    failure_modes_json TEXT NOT NULL DEFAULT '[]',
    counters_json TEXT NOT NULL DEFAULT '[]',
    source_claim_ids_json TEXT NOT NULL DEFAULT '[]',
    source_event_id TEXT,
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(
        module_status IN (
            'locked', 'available', 'enabled', 'suppressed',
            'deprecated', 'superseded'
        )
    ),
    CHECK(authority_status IN ('canon', 'planned', 'rumor', 'misread')),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(advantage_id)
        REFERENCES advantage_definitions(advantage_id)
);

CREATE INDEX IF NOT EXISTS idx_advantage_modules_advantage
    ON advantage_module_definitions(
        advantage_id, module_status, stage, module_id
    );

CREATE TABLE IF NOT EXISTS advantage_runtime_slots (
    slot_id TEXT PRIMARY KEY,
    advantage_id TEXT NOT NULL,
    module_id TEXT,
    stage TEXT NOT NULL,
    capacity REAL,
    unlock_graph_json TEXT NOT NULL DEFAULT '{}',
    set_membership_json TEXT NOT NULL DEFAULT '[]',
    authority_status TEXT NOT NULL DEFAULT 'canon',
    slot_status TEXT NOT NULL,
    source_claim_ids_json TEXT NOT NULL DEFAULT '[]',
    source_event_id TEXT,
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(
        capacity IS NULL
        OR (
            typeof(capacity) IN ('integer', 'real')
            AND capacity >= 0
            AND capacity <= 1.0e308
        )
    ),
    CHECK(slot_status IN ('locked', 'available', 'filled', 'disabled')),
    CHECK(authority_status IN ('canon', 'planned', 'rumor', 'misread')),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(advantage_id)
        REFERENCES advantage_definitions(advantage_id),
    FOREIGN KEY(module_id)
        REFERENCES advantage_module_definitions(module_id)
);

CREATE INDEX IF NOT EXISTS idx_advantage_slots_advantage
    ON advantage_runtime_slots(
        advantage_id, stage, slot_status, slot_id
    );

CREATE TABLE IF NOT EXISTS advantage_runtime_state (
    runtime_key TEXT PRIMARY KEY,
    advantage_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    owner_entity_id TEXT,
    stage TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    charges REAL,
    max_charges REAL,
    cooldown_until_json TEXT NOT NULL DEFAULT 'null',
    resources_json TEXT NOT NULL DEFAULT '{}',
    pollution REAL NOT NULL DEFAULT 0,
    exposure REAL NOT NULL DEFAULT 0,
    debt REAL NOT NULL DEFAULT 0,
    unlocked_modules_json TEXT NOT NULL DEFAULT '[]',
    runtime_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE(advantage_id, branch_id),
    CHECK(typeof(enabled) = 'integer' AND enabled IN (0, 1)),
    CHECK(
        charges IS NULL
        OR (
            typeof(charges) IN ('integer', 'real')
            AND charges >= 0
            AND charges <= 1.0e308
        )
    ),
    CHECK(
        max_charges IS NULL
        OR (
            typeof(max_charges) IN ('integer', 'real')
            AND max_charges >= 0
            AND max_charges <= 1.0e308
        )
    ),
    CHECK(charges IS NULL OR max_charges IS NULL OR charges <= max_charges),
    CHECK(
        typeof(pollution) IN ('integer', 'real')
        AND pollution >= 0 AND pollution <= 1.0e308
    ),
    CHECK(
        typeof(exposure) IN ('integer', 'real')
        AND exposure >= 0 AND exposure <= 1.0e308
    ),
    CHECK(
        typeof(debt) IN ('integer', 'real')
        AND debt >= 0 AND debt <= 1.0e308
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(advantage_id)
        REFERENCES advantage_definitions(advantage_id)
);

CREATE INDEX IF NOT EXISTS idx_advantage_runtime_owner
    ON advantage_runtime_state(
        owner_entity_id, enabled, branch_id, advantage_id
    );

CREATE TABLE IF NOT EXISTS advantage_ledger (
    entry_id TEXT PRIMARY KEY,
    advantage_id TEXT NOT NULL,
    module_id TEXT,
    branch_id TEXT NOT NULL,
    entry_kind TEXT NOT NULL,
    actor_entity_id TEXT,
    target_entity_id TEXT,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    loss_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    causal_event_id TEXT,
    source_event_id TEXT,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(
        length(trim(entry_kind)) BETWEEN 1 AND 256
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(advantage_id)
        REFERENCES advantage_definitions(advantage_id),
    FOREIGN KEY(module_id)
        REFERENCES advantage_module_definitions(module_id)
);

CREATE INDEX IF NOT EXISTS idx_advantage_ledger_advantage
    ON advantage_ledger(
        advantage_id, branch_id, updated_order DESC, entry_id
    );
CREATE INDEX IF NOT EXISTS idx_advantage_ledger_module
    ON advantage_ledger(module_id, updated_order DESC, entry_id);

CREATE TABLE IF NOT EXISTS advantage_knowledge (
    knowledge_id TEXT PRIMARY KEY,
    advantage_id TEXT NOT NULL,
    module_id TEXT,
    observer_entity_id TEXT,
    knowledge_plane TEXT NOT NULL,
    knowledge_status TEXT NOT NULL,
    claim_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    reveal_stage TEXT NOT NULL,
    misread_of TEXT,
    source_claim_ids_json TEXT NOT NULL DEFAULT '[]',
    source_event_id TEXT,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(
        knowledge_plane IN (
            'objective', 'actor_belief', 'public_narrative',
            'reader_disclosed', 'author_plan'
        )
    ),
    CHECK(
        knowledge_status IN ('canon', 'planned', 'rumor', 'misread')
    ),
    CHECK(
        typeof(confidence) IN ('integer', 'real')
        AND confidence >= 0 AND confidence <= 1
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(advantage_id)
        REFERENCES advantage_definitions(advantage_id),
    FOREIGN KEY(module_id)
        REFERENCES advantage_module_definitions(module_id),
    FOREIGN KEY(misread_of)
        REFERENCES advantage_knowledge(knowledge_id)
);

CREATE INDEX IF NOT EXISTS idx_advantage_knowledge_scope
    ON advantage_knowledge(
        advantage_id, knowledge_plane, observer_entity_id,
        knowledge_status, updated_order DESC
    );

CREATE TABLE IF NOT EXISTS advantage_contracts (
    contract_id TEXT PRIMARY KEY,
    advantage_id TEXT NOT NULL,
    actor_entity_id TEXT,
    counterparty_entity_id TEXT,
    authority_status TEXT NOT NULL DEFAULT 'canon',
    contract_status TEXT NOT NULL,
    terms_json TEXT NOT NULL DEFAULT '[]',
    agency_json TEXT NOT NULL DEFAULT '{}',
    trust REAL NOT NULL DEFAULT 0,
    debt REAL NOT NULL DEFAULT 0,
    breach_effect_json TEXT NOT NULL DEFAULT '{}',
    source_claim_ids_json TEXT NOT NULL DEFAULT '[]',
    source_event_id TEXT,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(
        contract_status IN (
            'proposed', 'active', 'suspended', 'breached',
            'fulfilled', 'terminated'
        )
    ),
    CHECK(authority_status IN ('canon', 'planned', 'rumor', 'misread')),
    CHECK(
        typeof(trust) IN ('integer', 'real')
        AND trust >= -1.0e308 AND trust <= 1.0e308
    ),
    CHECK(
        typeof(debt) IN ('integer', 'real')
        AND debt >= 0 AND debt <= 1.0e308
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(advantage_id)
        REFERENCES advantage_definitions(advantage_id)
);

CREATE INDEX IF NOT EXISTS idx_advantage_contracts_advantage
    ON advantage_contracts(
        advantage_id, contract_status, updated_order DESC, contract_id
    );

CREATE TABLE IF NOT EXISTS advantage_narrative_contracts (
    narrative_contract_id TEXT PRIMARY KEY,
    advantage_id TEXT NOT NULL UNIQUE,
    authority_status TEXT NOT NULL DEFAULT 'canon',
    contract_status TEXT NOT NULL,
    reading_promise_json TEXT NOT NULL DEFAULT '{}',
    reward_loop_json TEXT NOT NULL DEFAULT '[]',
    risk_loop_json TEXT NOT NULL DEFAULT '[]',
    reveal_ladder_json TEXT NOT NULL DEFAULT '[]',
    experience_binding_json TEXT NOT NULL DEFAULT '{}',
    source_claim_ids_json TEXT NOT NULL DEFAULT '[]',
    source_event_id TEXT,
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(contract_status IN ('active', 'planned', 'retired')),
    CHECK(authority_status IN ('canon', 'planned', 'rumor', 'misread')),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(advantage_id)
        REFERENCES advantage_definitions(advantage_id)
);

CREATE INDEX IF NOT EXISTS idx_advantage_narrative_status
    ON advantage_narrative_contracts(
        contract_status, advantage_id, narrative_contract_id
    );

-- This metadata is independent from continuity and item projection hashes.
CREATE TABLE IF NOT EXISTS advantage_projection_meta (
    meta_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    source_event_id TEXT,
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0)
);
"""


def canonical_json(value: object) -> str:
    """Return the deterministic JSON representation used by this projection."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_advantage_id(prefix: str, value: object) -> str:
    """Create a deterministic identifier from semantic content."""

    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return prefix + digest


_ADVANTAGE_LEDGER_REBUILD_SQL = r"""
CREATE TABLE advantage_ledger_entry_kind_v1 (
    entry_id TEXT PRIMARY KEY,
    advantage_id TEXT NOT NULL,
    module_id TEXT,
    branch_id TEXT NOT NULL,
    entry_kind TEXT NOT NULL,
    actor_entity_id TEXT,
    target_entity_id TEXT,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    loss_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    causal_event_id TEXT,
    source_event_id TEXT,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(length(trim(entry_kind)) BETWEEN 1 AND 256),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(advantage_id)
        REFERENCES advantage_definitions(advantage_id),
    FOREIGN KEY(module_id)
        REFERENCES advantage_module_definitions(module_id)
)
"""
_ADVANTAGE_LEDGER_COLUMNS = (
    "entry_id",
    "advantage_id",
    "module_id",
    "branch_id",
    "entry_kind",
    "actor_entity_id",
    "target_entity_id",
    "input_json",
    "output_json",
    "loss_json",
    "provenance_json",
    "causal_event_id",
    "source_event_id",
    "story_coordinate_json",
    "updated_order",
)


def _advantage_ledger_needs_entry_kind_migration(
    connection: sqlite3.Connection,
) -> bool:
    row = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type='table' AND name='advantage_ledger'
        """
    ).fetchone()
    if row is None or row[0] is None:
        return False
    normalized = " ".join(str(row[0]).lower().split())
    return "entry_kind in (" in normalized


def _migrate_advantage_ledger_entry_kind(
    connection: sqlite3.Connection,
) -> None:
    """Replace the closed ledger-kind CHECK without rewriting row values."""

    if not _advantage_ledger_needs_entry_kind_migration(connection):
        return
    connection.execute(
        "DROP TABLE IF EXISTS advantage_ledger_entry_kind_v1"
    )
    connection.execute(_ADVANTAGE_LEDGER_REBUILD_SQL)
    columns = ", ".join(f'"{column}"' for column in _ADVANTAGE_LEDGER_COLUMNS)
    connection.execute(
        f"""
        INSERT INTO advantage_ledger_entry_kind_v1 ({columns})
        SELECT {columns}
        FROM advantage_ledger
        """
    )
    connection.execute("DROP TABLE advantage_ledger")
    connection.execute(
        """
        ALTER TABLE advantage_ledger_entry_kind_v1
        RENAME TO advantage_ledger
        """
    )


def advantage_schema_ready(connection: sqlite3.Connection) -> bool:
    """Return whether every additive Advantage table and index is available."""

    required = set(ADVANTAGE_PROJECTION_TABLES) | {
        "advantage_projection_meta"
    }
    placeholders = ", ".join("?" for _ in required)
    rows = connection.execute(
        f"""
        SELECT name
        FROM sqlite_master
        WHERE type='table' AND name IN ({placeholders})
        """,
        tuple(sorted(required)),
    ).fetchall()
    if {str(row[0]) for row in rows} != required:
        return False
    index_placeholders = ", ".join(
        "?" for _ in ADVANTAGE_PROJECTION_INDEXES
    )
    index_rows = connection.execute(
        f"""
        SELECT name
        FROM sqlite_master
        WHERE type='index' AND name IN ({index_placeholders})
        """,
        tuple(sorted(ADVANTAGE_PROJECTION_INDEXES)),
    ).fetchall()
    if {
        str(row[0]) for row in index_rows
    } != set(ADVANTAGE_PROJECTION_INDEXES):
        return False
    if _advantage_ledger_needs_entry_kind_migration(connection):
        return False
    for table, columns in _ADVANTAGE_COLUMN_MIGRATIONS.items():
        existing = {
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        if not set(columns).issubset(existing):
            return False
    return True


def _execute_schema_statements(
    connection: sqlite3.Connection,
    sql: str,
) -> None:
    """Execute complete DDL statements without ``executescript`` commits."""

    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if not sqlite3.complete_statement(buffer):
            continue
        statement = buffer.strip()
        buffer = ""
        if statement:
            connection.execute(statement)
    if buffer.strip():
        raise RuntimeError("incomplete Advantage schema SQL statement")


def ensure_advantage_schema(connection: sqlite3.Connection) -> None:
    """Create the additive Advantage tables without committing the caller."""

    if advantage_schema_ready(connection):
        return
    query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
    if query_only:
        raise ContinuityError(
            "ADVANTAGE_SCHEMA_MISSING",
            "read-only connection does not contain Advantage tables",
        )
    _execute_schema_statements(connection, ADVANTAGE_SCHEMA_SQL)
    # ``CREATE TABLE IF NOT EXISTS`` cannot alter the legacy closed
    # ``entry_kind`` CHECK.  Rebuild that one table in-place, preserving every
    # row and foreign-key column, then recreate its indexes from the canonical
    # schema DDL.
    if _advantage_ledger_needs_entry_kind_migration(connection):
        _migrate_advantage_ledger_entry_kind(connection)
        _execute_schema_statements(connection, ADVANTAGE_SCHEMA_SQL)
    for table, columns in _ADVANTAGE_COLUMN_MIGRATIONS.items():
        existing = {
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        for column, definition in columns.items():
            if column not in existing:
                connection.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN '
                    f'"{column}" {definition}'
                )


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    ]


def _decode_json(value: Any, fallback: Any) -> Any:
    if value is None or value == "":
        return deepcopy(fallback)
    if isinstance(value, (dict, list, int, float, bool)):
        return deepcopy(value)
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return deepcopy(fallback)


def _decoded_row(
    row: sqlite3.Row | Mapping[str, Any] | Sequence[Any],
    *,
    columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    if isinstance(row, Mapping) or isinstance(row, sqlite3.Row):
        result = dict(row)
    elif columns is not None:
        result = {
            column: row[index]
            for index, column in enumerate(columns)
        }
    else:
        raise TypeError("columns are required for tuple rows")
    for column in _JSON_COLUMNS.intersection(result):
        fallback: Any = None if column == "cooldown_until_json" else {}
        if column.endswith("_ids_json") or column in {
            "profiles_json",
            "counterplay_json",
            "preconditions_json",
            "targets_json",
            "costs_json",
            "effects_json",
            "side_effects_json",
            "failure_modes_json",
            "counters_json",
            "set_membership_json",
            "unlocked_modules_json",
            "terms_json",
            "reward_loop_json",
            "risk_loop_json",
            "reveal_ladder_json",
        }:
            fallback = []
        result[column] = _decode_json(result[column], fallback)
    if "enabled" in result and isinstance(result["enabled"], (bool, int)):
        result["enabled"] = bool(result["enabled"])
    return result


def _serialize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for column in _JSON_COLUMNS.intersection(result):
        value = result[column]
        if isinstance(value, str):
            try:
                json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                result[column] = canonical_json(value)
        else:
            result[column] = canonical_json(value)
    return result


def _insert_row(
    connection: sqlite3.Connection,
    table: str,
    row: Mapping[str, Any],
) -> None:
    columns = _table_columns(connection, table)
    serialized = _serialize_row(row)
    selected = [column for column in columns if column in serialized]
    placeholders = ", ".join("?" for _ in selected)
    connection.execute(
        f'INSERT INTO "{table}"({", ".join(selected)}) '
        f"VALUES({placeholders})",
        tuple(serialized[column] for column in selected),
    )


def _required_text(
    value: Any,
    *,
    field: str,
    code: str = "ADVANTAGE_FIELD_REQUIRED",
) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContinuityError(
            code,
            f"{field} is required",
            details={"field": field},
        )
    return text


def _finite_number(
    value: Any,
    *,
    field: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContinuityError(
            "ADVANTAGE_INVALID_NUMBER",
            f"{field} must be numeric",
            details={"field": field, "value": value},
        ) from exc
    if not math.isfinite(number):
        raise ContinuityError(
            "ADVANTAGE_INVALID_NUMBER",
            f"{field} must be finite",
            details={"field": field, "value": value},
        )
    if minimum is not None and number < minimum - _EPSILON:
        raise ContinuityError(
            "ADVANTAGE_CONSERVATION_VIOLATION",
            f"{field} cannot be below {minimum}",
            details={"field": field, "value": number, "minimum": minimum},
        )
    if maximum is not None and number > maximum + _EPSILON:
        raise ContinuityError(
            "ADVANTAGE_CONSERVATION_VIOLATION",
            f"{field} cannot exceed {maximum}",
            details={"field": field, "value": number, "maximum": maximum},
        )
    if abs(number) <= _EPSILON:
        return 0.0
    return number


def _as_object(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContinuityError(
            "ADVANTAGE_INVALID_OBJECT",
            f"{field} must be an object",
            details={"field": field},
        )
    return deepcopy(dict(value))


def _as_list(value: Any, *, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ContinuityError(
            "ADVANTAGE_INVALID_ARRAY",
            f"{field} must be an array",
            details={"field": field},
        )
    return deepcopy(value)


def _contains_author_plan(value: Any) -> bool:
    """Return whether a nested value carries an author-plan marker."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            marker = str(key).strip().casefold().replace("-", "_")
            if marker == "author_plan" or _contains_author_plan(nested):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_author_plan(item) for item in value)
    if isinstance(value, str):
        return value.strip().casefold().replace("-", "_") == "author_plan"
    return False


def _generation_safe_payload(value: Any) -> Any:
    """Remove author-side reveal scheduling keys from business payloads."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, nested in value.items():
            marker = str(key).strip().casefold().replace("-", "_")
            if (
                marker == "author_plan"
                or marker.startswith("author_")
                or marker.startswith("planned_")
                or marker.startswith("future_")
                or marker.startswith("reveal_")
                or marker
                in {
                    "reveal_stage",
                    "reveal_ladder",
                    "reveal_schedule",
                }
            ):
                continue
            sanitized = _generation_safe_payload(nested)
            if sanitized is not _GENERATION_REDACTED:
                result[str(key)] = sanitized
        return result
    if isinstance(value, list):
        return [
            sanitized
            for item in value
            for sanitized in [_generation_safe_payload(item)]
            if sanitized is not _GENERATION_REDACTED
        ]
    if isinstance(value, tuple):
        return _generation_safe_payload(list(value))
    if isinstance(value, str) and (
        value.strip().casefold().replace("-", "_") == "author_plan"
    ):
        return _GENERATION_REDACTED
    return deepcopy(value)


def _string_list(value: Any, *, field: str) -> list[str]:
    return sorted(
        {
            _required_text(item, field=field)
            for item in _as_list(value, field=field)
        }
    )


def _coordinate(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    calendar_id = str(value.get("calendar_id") or "").strip()
    ordinal = value.get("ordinal")
    if not calendar_id or type(ordinal) is not int:
        return None
    result: dict[str, Any] = {
        "calendar_id": calendar_id,
        "ordinal": int(ordinal),
    }
    for key in ("label", "precision", "source_event_id"):
        if value.get(key) is not None:
            result[key] = str(value[key])
    return result


def _normalize_story_cursor(
    value: Any = None,
    *,
    chapter_no: int | None = None,
    scene_index: int | None = None,
) -> dict[str, Any] | None:
    """Normalize the accepted story position used by generation queries.

    Advantage events use a ``calendar_id``/``ordinal`` coordinate.  Hook
    callers commonly only have chapter and scene numbers, so those are mapped
    to the same deterministic ``chapter_scene`` calendar used by the power
    runtime.  A malformed or partial cursor is intentionally treated as
    absent; callers can then choose to fail closed instead of comparing
    incomparable positions.
    """

    candidate = value
    if isinstance(candidate, Mapping):
        normalized = _coordinate(candidate)
        if normalized is not None:
            return normalized
        # Accept a compact ``{"ordinal": N}`` cursor only when its calendar
        # is explicit in a sibling key; silently guessing a calendar would
        # allow a future event from another timeline to pass the gate.
        calendar_id = str(candidate.get("calendar_id") or "").strip()
        ordinal = candidate.get("ordinal")
        if calendar_id and type(ordinal) is int:
            return {
                "calendar_id": calendar_id,
                "ordinal": int(ordinal),
            }
    if chapter_no is None:
        return None
    try:
        chapter = int(chapter_no)
        scene = int(scene_index or 0)
    except (TypeError, ValueError):
        return None
    if chapter < 0 or scene < 0:
        return None
    return {
        "calendar_id": "chapter_scene",
        "ordinal": chapter * 1_000_000 + scene,
        "label": f"chapter={chapter},scene={scene}",
        "precision": "scene",
    }


def _row_story_coordinate(row: Mapping[str, Any]) -> dict[str, Any] | None:
    return _coordinate(
        row.get("story_coordinate_json")
        or row.get("story_coordinate")
    )


def _is_timeless_bootstrap_row(row: Mapping[str, Any]) -> bool:
    """Return whether a projection row is a timeless sidecar bootstrap.

    Sidecar bootstrap records intentionally have no accepted story coordinate
    and no source event.  They describe the initial world rather than a future
    event, so a chapter/scene cursor must not make them disappear.  Rows with
    an event id remain fail-closed when their coordinate is incomparable.
    """

    return (
        _row_story_coordinate(row) is None
        and not str(row.get("source_event_id") or "").strip()
    )


def _coordinate_visible(
    row: Mapping[str, Any],
    cursor: Mapping[str, Any] | None,
    *,
    allow_missing_without_cursor: bool = True,
) -> bool:
    """Return whether a row is provably at or before ``cursor``.

    With a cursor present, missing coordinates and cross-calendar rows are
    hidden rather than guessed.  This is the critical fail-closed behavior
    that prevents a current projection from leaking a later chapter's state.
    """

    if cursor is None:
        return bool(allow_missing_without_cursor)
    current = _coordinate(cursor)
    row_coordinate = _row_story_coordinate(row)
    if current is None:
        return False
    if row_coordinate is None:
        return _is_timeless_bootstrap_row(row)
    if row_coordinate["calendar_id"] != current["calendar_id"]:
        return False
    return int(row_coordinate["ordinal"]) <= int(current["ordinal"])


def _reveal_stage_visible(
    row: Mapping[str, Any],
    *,
    cursor: Mapping[str, Any] | None,
    reveal_stage: str | None = None,
    visible_reveal_stages: Sequence[str] | None = None,
) -> bool:
    stage = str(row.get("reveal_stage") or "").strip()
    folded = stage.casefold()
    requested = (
        str(reveal_stage).strip().casefold()
        if reveal_stage is not None
        else None
    )
    if requested is not None and folded != requested:
        return False
    if not stage:
        return False
    explicit = (
        {
            str(item).strip().casefold()
            for item in visible_reveal_stages or ()
            if str(item).strip()
        }
        if visible_reveal_stages is not None
        else None
    )
    if folded in ADVANTAGE_HIDDEN_REVEAL_STAGES:
        return False
    if any(
        marker in folded
        for marker in (
            "author",
            "future",
            "hidden",
            "planned",
            "deferred",
            "unknown",
            "unrevealed",
        )
    ):
        return False
    stage_whitelist = (
        explicit
        if explicit is not None
        else ADVANTAGE_DEFAULT_VISIBLE_REVEAL_STAGES
    )
    if cursor is not None:
        row_coordinate = _row_story_coordinate(row)
        current = _coordinate(cursor)
        # Comparable accepted coordinates are authoritative and may carry
        # project-specific stage labels.  Timeless or cross-calendar rows fall
        # back to the conservative current-head stage allow-list.
        if (
            current is not None
            and row_coordinate is not None
            and row_coordinate.get("calendar_id") == current.get("calendar_id")
        ):
            return int(row_coordinate["ordinal"]) <= int(current["ordinal"])
        return folded in stage_whitelist
    # Without a cursor only the explicit project allow-list or the conservative
    # built-in current-head stages are accepted.
    return folded in stage_whitelist


def _event_payload(
    row: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    mapped = dict(row)
    event_type = str(mapped.get("event_type") or "")
    payload = dict(_decode_json(mapped.get("payload_json"), {}))
    if not payload:
        payload = {
            key: deepcopy(value)
            for key, value in mapped.items()
            if key not in {"payload_json", "changes_authority", "updated_order"}
        }
    if event_type == "advantage_correction":
        if str(payload.get("action") or "") == "retract":
            return event_type, {}
        replacement = payload.get("replacement")
        if not isinstance(replacement, Mapping):
            return event_type, {}
        payload = dict(replacement)
        event_type = str(payload.get("event_type") or "")
    return event_type, payload


def _meta_int(
    connection: sqlite3.Connection,
    key: str,
    default: int = 0,
) -> int:
    try:
        row = connection.execute(
            "SELECT value FROM state_meta WHERE key=?",
            (key,),
        ).fetchone()
    except sqlite3.OperationalError:
        return int(default)
    return int(row[0]) if row is not None else int(default)


def advantage_projection_payload(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    """Read the stable main-branch Advantage projection payload.

    Provisional branch runtime and ledger rows remain queryable by ``branch_id``
    but are intentionally outside the canonical main projection hash.
    """

    ensure_advantage_schema(connection)
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in ADVANTAGE_PROJECTION_TABLES:
        columns = _table_columns(connection, table)
        stable_columns = [
            column
            for column in columns
            if column not in {"created_at", "updated_at", "completed_at"}
        ]
        if not stable_columns:
            tables[table] = []
            continue
        selected = ", ".join(f'"{column}"' for column in stable_columns)
        branch_filter = (
            " WHERE branch_id='main'"
            if table in {"advantage_runtime_state", "advantage_ledger"}
            else ""
        )
        rows = connection.execute(
            f'SELECT {selected} FROM "{table}"'
            f"{branch_filter} ORDER BY {selected}"
        ).fetchall()
        tables[table] = [
            _decoded_row(row, columns=stable_columns)
            for row in rows
        ]
    return {
        "schema_version": ADVANTAGE_PROJECTION_SCHEMA_VERSION,
        "tables": tables,
    }


def compute_advantage_projection_hash(
    connection: sqlite3.Connection,
) -> str:
    """Hash only the stable Advantage projection tables."""

    payload = advantage_projection_payload(connection)
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return "advantage_projection_" + digest


def refresh_advantage_projection_metadata(
    connection: sqlite3.Connection,
    *,
    source_event_id: str | None = None,
    updated_order: int | None = None,
) -> str:
    """Persist the independent Advantage projection version and hash."""

    ensure_advantage_schema(connection)
    if updated_order is None:
        maxima = [
            connection.execute(
                f'SELECT COALESCE(MAX(updated_order), 0) FROM "{table}"'
            ).fetchone()[0]
            for table in ADVANTAGE_PROJECTION_TABLES
        ]
        updated_order = max(int(value or 0) for value in maxima)
    projection_hash = compute_advantage_projection_hash(connection)
    values: tuple[tuple[str, Any], ...] = (
        (ADVANTAGE_META_VERSION, ADVANTAGE_PROJECTION_SCHEMA_VERSION),
        (ADVANTAGE_META_HASH, projection_hash),
        (
            ADVANTAGE_META_HEAD_REVISION,
            _meta_int(connection, "head_canon_revision"),
        ),
        (
            ADVANTAGE_META_ACTIVE_REVISION,
            _meta_int(connection, "active_canon_revision"),
        ),
    )
    for key, value in values:
        connection.execute(
            """
            INSERT INTO advantage_projection_meta(
                meta_key, value_json, source_event_id, updated_order
            ) VALUES(?, ?, ?, ?)
            ON CONFLICT(meta_key) DO UPDATE SET
                value_json=excluded.value_json,
                source_event_id=excluded.source_event_id,
                updated_order=excluded.updated_order
            """,
            (
                key,
                canonical_json(value),
                source_event_id,
                int(updated_order),
            ),
        )
    return projection_hash


def read_advantage_projection_metadata(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    """Read decoded Advantage projection metadata."""

    ensure_advantage_schema(connection)
    return {
        str(row[0]): _decode_json(row[1], row[1])
        for row in connection.execute(
            """
            SELECT meta_key, value_json
            FROM advantage_projection_meta
            ORDER BY meta_key
            """
        )
    }


class AdvantageProjectionState:
    """Pure reducer shared by strict validation, bootstrap and replay."""

    def __init__(self) -> None:
        self.definitions: dict[str, dict[str, Any]] = {}
        self.anchors: dict[str, dict[str, Any]] = {}
        self.modules: dict[str, dict[str, Any]] = {}
        self.slots: dict[str, dict[str, Any]] = {}
        self.runtime: dict[tuple[str, str], dict[str, Any]] = {}
        self.ledger: dict[str, dict[str, Any]] = {}
        self.knowledge: dict[str, dict[str, Any]] = {}
        self.contracts: dict[str, dict[str, Any]] = {}
        self.narrative_contracts: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_connection(
        cls,
        connection: sqlite3.Connection,
    ) -> "AdvantageProjectionState":
        ensure_advantage_schema(connection)
        state = cls()
        targets: tuple[
            tuple[str, dict[Any, dict[str, Any]], str], ...
        ] = (
            (
                "advantage_definitions",
                state.definitions,
                "advantage_id",
            ),
            ("advantage_anchors", state.anchors, "anchor_id"),
            (
                "advantage_module_definitions",
                state.modules,
                "module_id",
            ),
            ("advantage_runtime_slots", state.slots, "slot_id"),
            ("advantage_ledger", state.ledger, "entry_id"),
            ("advantage_knowledge", state.knowledge, "knowledge_id"),
            ("advantage_contracts", state.contracts, "contract_id"),
            (
                "advantage_narrative_contracts",
                state.narrative_contracts,
                "narrative_contract_id",
            ),
        )
        for table, target, key_column in targets:
            columns = _table_columns(connection, table)
            for raw_row in connection.execute(
                f'SELECT * FROM "{table}" ORDER BY "{key_column}"'
            ):
                row = _decoded_row(raw_row, columns=columns)
                target[str(row[key_column])] = row
        columns = _table_columns(connection, "advantage_runtime_state")
        for raw_row in connection.execute(
            """
            SELECT * FROM advantage_runtime_state
            ORDER BY advantage_id, branch_id
            """
        ):
            row = _decoded_row(raw_row, columns=columns)
            state.runtime[
                (str(row["advantage_id"]), str(row["branch_id"]))
            ] = row
        return state

    def persist(self, connection: sqlite3.Connection) -> None:
        ensure_advantage_schema(connection)
        for table in _ADVANTAGE_TABLE_DELETE_ORDER:
            connection.execute(f'DELETE FROM "{table}"')
        knowledge_rows = self._ordered_knowledge_rows()
        rows_by_table: tuple[
            tuple[str, Iterable[dict[str, Any]]], ...
        ] = (
            ("advantage_definitions", self.definitions.values()),
            ("advantage_anchors", self.anchors.values()),
            ("advantage_module_definitions", self.modules.values()),
            ("advantage_runtime_slots", self.slots.values()),
            ("advantage_runtime_state", self.runtime.values()),
            ("advantage_ledger", self.ledger.values()),
            ("advantage_knowledge", knowledge_rows),
            ("advantage_contracts", self.contracts.values()),
            (
                "advantage_narrative_contracts",
                self.narrative_contracts.values(),
            ),
        )
        for table, rows in rows_by_table:
            ordered_rows = (
                list(rows)
                if table == "advantage_knowledge"
                else sorted(rows, key=canonical_json)
            )
            for row in ordered_rows:
                _insert_row(connection, table, row)

    def _ordered_knowledge_rows(self) -> list[dict[str, Any]]:
        """Topologically order misreads after the claims they reference."""

        remaining = dict(self.knowledge)
        ordered: list[dict[str, Any]] = []
        emitted: set[str] = set()
        while remaining:
            ready = [
                key
                for key, row in remaining.items()
                if not row.get("misread_of")
                or str(row["misread_of"]) in emitted
            ]
            if not ready:
                missing = sorted(
                    {
                        str(row.get("misread_of"))
                        for row in remaining.values()
                        if row.get("misread_of")
                        and str(row.get("misread_of")) not in self.knowledge
                    }
                )
                raise ContinuityError(
                    "ADVANTAGE_KNOWLEDGE_REFERENCE_INVALID",
                    (
                        "knowledge misread reference is missing"
                        if missing
                        else "knowledge misread references contain a cycle"
                    ),
                    details={
                        "missing": missing,
                        "remaining": sorted(remaining),
                    },
                )
            for key in sorted(ready):
                ordered.append(remaining.pop(key))
                emitted.add(key)
        return ordered

    def _definition(self, advantage_id: str) -> dict[str, Any]:
        row = self.definitions.get(advantage_id)
        if row is None:
            raise ContinuityError(
                "ADVANTAGE_NOT_FOUND",
                f"unknown advantage: {advantage_id}",
                details={"advantage_id": advantage_id},
            )
        return row

    def _module(
        self,
        advantage_id: str,
        module_id: str,
        *,
        usable: bool = False,
    ) -> dict[str, Any]:
        row = self.modules.get(module_id)
        if row is None or str(row.get("advantage_id")) != advantage_id:
            raise ContinuityError(
                "ADVANTAGE_MODULE_NOT_FOUND",
                f"unknown module for advantage: {module_id}",
                details={
                    "advantage_id": advantage_id,
                    "module_id": module_id,
                },
            )
        if usable and str(row.get("authority_status") or "canon") != "canon":
            raise ContinuityError(
                "ADVANTAGE_NONCANON_AUTHORITY",
                "non-canon advantage module cannot be used",
                details={
                    "advantage_id": advantage_id,
                    "module_id": module_id,
                    "status": row.get("authority_status"),
                },
            )
        if usable and str(row.get("module_status")) not in {
            "available",
            "enabled",
        }:
            raise ContinuityError(
                "ADVANTAGE_MODULE_UNAVAILABLE",
                "advantage module is not currently usable",
                details={
                    "advantage_id": advantage_id,
                    "module_id": module_id,
                    "module_status": row.get("module_status"),
                },
            )
        return row

    def _ensure_runtime(
        self,
        advantage_id: str,
        *,
        branch_id: str,
        source_event_id: str | None,
        updated_order: int,
        coordinate: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        key = (advantage_id, branch_id)
        row = self.runtime.get(key)
        if row is not None:
            return row
        if branch_id != "main":
            main_runtime = self.runtime.get((advantage_id, "main"))
            if main_runtime is not None:
                row = deepcopy(main_runtime)
                row.update(
                    {
                        "runtime_key": stable_advantage_id(
                            "advantage_runtime_",
                            {
                                "advantage_id": advantage_id,
                                "branch_id": branch_id,
                            },
                        ),
                        "branch_id": branch_id,
                        "source_event_id": source_event_id,
                        "story_coordinate_json": dict(
                            coordinate
                            or main_runtime.get("story_coordinate_json")
                            or {}
                        ),
                        "updated_order": int(updated_order),
                    }
                )
                self.runtime[key] = row
                return row
        definition = self._definition(advantage_id)
        definition_payload = _as_object(
            definition.get("definition_json"),
            field="definition_json",
        )
        max_charges_raw = definition_payload.get("max_charges")
        max_charges = (
            _finite_number(
                max_charges_raw,
                field="max_charges",
                minimum=0,
            )
            if max_charges_raw is not None
            else None
        )
        charges_raw = definition_payload.get("initial_charges", max_charges)
        charges = (
            _finite_number(charges_raw, field="charges", minimum=0)
            if charges_raw is not None
            else None
        )
        if (
            charges is not None
            and max_charges is not None
            and charges > max_charges + _EPSILON
        ):
            raise ContinuityError(
                "ADVANTAGE_CONSERVATION_VIOLATION",
                "initial charges exceed max_charges",
            )
        row = {
            "runtime_key": stable_advantage_id(
                "advantage_runtime_",
                {"advantage_id": advantage_id, "branch_id": branch_id},
            ),
            "advantage_id": advantage_id,
            "branch_id": branch_id,
            "owner_entity_id": None,
            "stage": str(definition_payload.get("initial_stage") or "dormant"),
            "enabled": 0,
            "charges": charges,
            "max_charges": max_charges,
            "cooldown_until_json": None,
            "resources_json": {
                str(key): _finite_number(
                    value,
                    field=f"initial_resources.{key}",
                    minimum=0,
                )
                for key, value in dict(
                    definition_payload.get("initial_resources") or {}
                ).items()
            },
            "pollution": 0.0,
            "exposure": 0.0,
            "debt": 0.0,
            "unlocked_modules_json": [],
            "runtime_json": {},
            "source_event_id": source_event_id,
            "story_coordinate_json": dict(coordinate or {}),
            "updated_order": int(updated_order),
        }
        self.runtime[key] = row
        return row

    @staticmethod
    def _assert_not_computed(event: Mapping[str, Any]) -> None:
        forbidden = {
            "before",
            "after",
            "before_state",
            "after_state",
            "resulting_state",
            "computed_state",
        }.intersection(event)
        if forbidden:
            raise ContinuityError(
                "ADVANTAGE_COMPUTED_STATE_FORBIDDEN",
                "computed before/after state is local reducer output",
                details={"fields": sorted(forbidden)},
            )

    @staticmethod
    def _branch(event: Mapping[str, Any]) -> str:
        return _required_text(
            event.get("branch_id") or "main",
            field="branch_id",
        )

    @staticmethod
    def _claims(event: Mapping[str, Any]) -> list[str]:
        return _string_list(
            event.get("source_claim_ids") or [],
            field="source_claim_ids",
        )

    @staticmethod
    def _event_coordinate(
        event: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        return _coordinate(event.get("story_coordinate"))

    def _apply_spec(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str | None,
        updated_order: int,
    ) -> None:
        action = str(event.get("action") or "define")
        spec_type = str(event.get("spec_type") or "advantage_definition")
        if spec_type == "runtime_slot":
            self._apply_slot(
                event,
                source_event_id=source_event_id,
                updated_order=updated_order,
            )
            return
        if spec_type == "narrative_contract":
            self._apply_narrative_contract(
                event,
                source_event_id=source_event_id,
                updated_order=updated_order,
            )
            return
        if spec_type != "advantage_definition":
            raise ContinuityError(
                "ADVANTAGE_SPEC_TYPE_INVALID",
                f"unsupported advantage spec type: {spec_type}",
            )
        advantage_id = _required_text(
            event.get("advantage_id") or event.get("spec_id"),
            field="advantage_id",
        )
        if action in {"deprecate", "supersede"}:
            row = self._definition(advantage_id)
            row["lifecycle_status"] = (
                "deprecated" if action == "deprecate" else "superseded"
            )
            row["source_event_id"] = source_event_id
            row["updated_order"] = int(updated_order)
            return
        if action not in {"define", "update"}:
            raise ContinuityError(
                "ADVANTAGE_ACTION_INVALID",
                f"unsupported advantage definition action: {action}",
            )
        definition = _as_object(
            event.get("definition"),
            field="definition",
        )
        existing = self.definitions.get(advantage_id)
        if action == "update" and existing is None:
            raise ContinuityError(
                "ADVANTAGE_NOT_FOUND",
                f"cannot update unknown advantage: {advantage_id}",
            )
        merged = (
            _as_object(existing.get("definition_json"), field="definition_json")
            if existing
            else {}
        )
        merged.update(definition)
        status = str(
            event.get("status")
            or merged.get("status")
            or (existing or {}).get("advantage_status")
            or "canon"
        )
        if status not in ADVANTAGE_STATUSES:
            raise ContinuityError(
                "ADVANTAGE_STATUS_INVALID",
                f"unsupported advantage status: {status}",
            )
        anchor_type = str(
            event.get("anchor_type")
            or merged.get("anchor_type")
            or (existing or {}).get("anchor_type")
            or ""
        )
        if anchor_type not in ADVANTAGE_ANCHOR_TYPES:
            raise ContinuityError(
                "ADVANTAGE_ANCHOR_TYPE_INVALID",
                f"unsupported anchor type: {anchor_type}",
            )
        row = {
            "advantage_id": advantage_id,
            "title": _required_text(
                event.get("title")
                or merged.get("title")
                or (existing or {}).get("title"),
                field="title",
            ),
            "profiles_json": _string_list(
                event.get("profiles")
                if event.get("profiles") is not None
                else merged.get("profiles")
                if merged.get("profiles") is not None
                else (existing or {}).get("profiles_json")
                or [],
                field="profiles",
            ),
            "anchor_type": anchor_type,
            "acquisition_mode": _required_text(
                event.get("acquisition_mode")
                or merged.get("acquisition_mode")
                or (existing or {}).get("acquisition_mode")
                or "unknown",
                field="acquisition_mode",
            ),
            "uniqueness": _required_text(
                event.get("uniqueness")
                or merged.get("uniqueness")
                or (existing or {}).get("uniqueness")
                or "unknown",
                field="uniqueness",
            ),
            "advantage_status": status,
            "lifecycle_status": str(
                (existing or {}).get("lifecycle_status") or "active"
            ),
            "promise_json": deepcopy(
                event.get("promise")
                if event.get("promise") is not None
                else merged.get("promise")
                if merged.get("promise") is not None
                else (existing or {}).get("promise_json")
                or {}
            ),
            "counterplay_json": deepcopy(
                event.get("counterplay")
                if event.get("counterplay") is not None
                else merged.get("counterplay")
                if merged.get("counterplay") is not None
                else (existing or {}).get("counterplay_json")
                or []
            ),
            "definition_json": merged,
            "source_claim_ids_json": self._claims(event)
            or deepcopy((existing or {}).get("source_claim_ids_json") or []),
            "source_event_id": source_event_id,
            "updated_order": int(updated_order),
        }
        self.definitions[advantage_id] = row

    def _apply_anchor(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str | None,
        updated_order: int,
    ) -> None:
        advantage_id = _required_text(
            event.get("advantage_id"),
            field="advantage_id",
        )
        definition = self._definition(advantage_id)
        anchor_id = _required_text(
            event.get("anchor_id"),
            field="anchor_id",
        )
        action = str(event.get("action") or "define")
        if action in {"deprecate", "supersede"}:
            row = self.anchors.get(anchor_id)
            if row is None:
                raise ContinuityError(
                    "ADVANTAGE_ANCHOR_NOT_FOUND",
                    f"unknown advantage anchor: {anchor_id}",
                )
            row["anchor_status"] = (
                "deprecated" if action == "deprecate" else "superseded"
            )
            row["updated_order"] = int(updated_order)
            row["source_event_id"] = source_event_id
            return
        anchor_type = str(
            event.get("anchor_type") or definition.get("anchor_type") or ""
        )
        if anchor_type not in ADVANTAGE_ANCHOR_TYPES:
            raise ContinuityError(
                "ADVANTAGE_ANCHOR_TYPE_INVALID",
                f"unsupported anchor type: {anchor_type}",
            )
        binding_state = str(event.get("binding_state") or "unbound")
        if binding_state not in {
            "unbound",
            "bound",
            "dormant",
            "sealed",
            "contested",
            "released",
        }:
            raise ContinuityError(
                "ADVANTAGE_BINDING_STATE_INVALID",
                f"unsupported binding state: {binding_state}",
            )
        existing = self.anchors.get(anchor_id)
        authority_status = str(
            event.get("status")
            or event.get("authority_status")
            or (existing or {}).get("authority_status")
            or (
                "planned"
                if str(event.get("scope") or "") in {"planned", "author_plan"}
                else "canon"
            )
        )
        if authority_status not in ADVANTAGE_STATUSES:
            raise ContinuityError(
                "ADVANTAGE_STATUS_INVALID",
                f"unsupported anchor authority status: {authority_status}",
            )
        self.anchors[anchor_id] = {
            "anchor_id": anchor_id,
            "advantage_id": advantage_id,
            "anchor_type": anchor_type,
            "anchor_ref_id": _required_text(
                event.get("anchor_ref_id")
                or event.get("subject_id")
                or (existing or {}).get("anchor_ref_id"),
                field="anchor_ref_id",
            ),
            "owner_entity_id": (
                event.get("owner_entity_id")
                if "owner_entity_id" in event
                else (existing or {}).get("owner_entity_id")
            ),
            "binding_state": binding_state,
            "transfer_rule_json": deepcopy(
                event.get("transfer_rule")
                if event.get("transfer_rule") is not None
                else (existing or {}).get("transfer_rule_json")
                or {}
            ),
            "authority_status": authority_status,
            "anchor_status": str(
                event.get("anchor_status")
                or (existing or {}).get("anchor_status")
                or "active"
            ),
            "attributes_json": deepcopy(
                event.get("attributes")
                if event.get("attributes") is not None
                else (existing or {}).get("attributes_json")
                or {}
            ),
            "source_claim_ids_json": self._claims(event)
            or deepcopy((existing or {}).get("source_claim_ids_json") or []),
            "source_event_id": source_event_id,
            "story_coordinate_json": dict(
                self._event_coordinate(event)
                or (existing or {}).get("story_coordinate_json")
                or {}
            ),
            "updated_order": int(updated_order),
        }

    def _apply_module(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str | None,
        updated_order: int,
    ) -> None:
        advantage_id = _required_text(
            event.get("advantage_id"),
            field="advantage_id",
        )
        self._definition(advantage_id)
        module_id = _required_text(event.get("module_id"), field="module_id")
        action = str(event.get("action") or "define")
        existing = self.modules.get(module_id)
        if action in {"unlock", "enable", "lock", "suppress", "deprecate"}:
            if existing is None:
                raise ContinuityError(
                    "ADVANTAGE_MODULE_NOT_FOUND",
                    f"unknown advantage module: {module_id}",
                )
            if (
                action in {"unlock", "enable"}
                and str(existing.get("authority_status")) != "canon"
            ):
                raise ContinuityError(
                    "ADVANTAGE_NONCANON_AUTHORITY",
                    "non-canon module cannot be unlocked or enabled",
                    details={
                        "module_id": module_id,
                        "status": existing.get("authority_status"),
                    },
                )
            statuses = {
                "unlock": "available",
                "enable": "enabled",
                "lock": "locked",
                "suppress": "suppressed",
                "deprecate": "deprecated",
            }
            branch_id = self._branch(event)
            if branch_id == "main":
                existing["module_status"] = statuses[action]
                existing["source_event_id"] = source_event_id
                existing["updated_order"] = int(updated_order)
            runtime = self._ensure_runtime(
                advantage_id,
                branch_id=branch_id,
                source_event_id=source_event_id,
                updated_order=updated_order,
                coordinate=self._event_coordinate(event),
            )
            unlocked = set(runtime.get("unlocked_modules_json") or [])
            if action in {"unlock", "enable"}:
                unlocked.add(module_id)
            elif action in {"lock", "suppress", "deprecate"}:
                unlocked.discard(module_id)
            runtime["unlocked_modules_json"] = sorted(unlocked)
            runtime["source_event_id"] = source_event_id
            runtime["updated_order"] = int(updated_order)
            return
        if action not in {"define", "update"}:
            raise ContinuityError(
                "ADVANTAGE_ACTION_INVALID",
                f"unsupported advantage module action: {action}",
            )
        definition = _as_object(
            event.get("definition"),
            field="definition",
        )
        def choose(name: str, fallback: Any) -> Any:
            if name in event:
                return deepcopy(event[name])
            if name in definition:
                return deepcopy(definition[name])
            if existing is not None:
                column = (
                    "module_kind"
                    if name in {"kind", "module_kind"}
                    else f"{name}_json"
                )
                if column in existing:
                    return deepcopy(existing[column])
            return deepcopy(fallback)

        authority_status = str(
            event.get("authority_status")
            or event.get("status")
            or definition.get("authority_status")
            or definition.get("status")
            or (existing or {}).get("authority_status")
            or (
                "planned"
                if str(event.get("scope") or "") in {"planned", "author_plan"}
                else "canon"
            )
        )
        if authority_status not in ADVANTAGE_STATUSES:
            raise ContinuityError(
                "ADVANTAGE_STATUS_INVALID",
                f"unsupported module authority status: {authority_status}",
            )
        status = str(
            event.get("module_status")
            or definition.get("module_status")
            or (existing or {}).get("module_status")
            or ("available" if authority_status == "canon" else "locked")
        )
        if status not in {
            "locked",
            "available",
            "enabled",
            "suppressed",
            "deprecated",
            "superseded",
        }:
            raise ContinuityError(
                "ADVANTAGE_MODULE_STATUS_INVALID",
                f"unsupported module status: {status}",
            )
        row = {
            "module_id": module_id,
            "advantage_id": advantage_id,
            "title": _required_text(
                event.get("title")
                or definition.get("title")
                or (existing or {}).get("title")
                or module_id,
                field="module.title",
            ),
            "module_kind": _required_text(
                event.get("kind")
                or event.get("module_kind")
                or definition.get("kind")
                or definition.get("module_kind")
                or (existing or {}).get("module_kind")
                or "ability",
                field="module.kind",
            ),
            "authority_status": authority_status,
            "module_status": status,
            "stage": _required_text(
                event.get("stage")
                or definition.get("stage")
                or (existing or {}).get("stage")
                or "initial",
                field="module.stage",
            ),
            "experience_contract_json": deepcopy(
                event.get("experience_contract")
                or definition.get("experience_contract")
                or (existing or {}).get("experience_contract_json")
                or {}
            ),
            "trigger_json": choose("trigger", {}),
            "preconditions_json": choose("preconditions", []),
            "targets_json": choose("targets", []),
            "costs_json": choose("costs", []),
            "effects_json": choose("effects", []),
            "side_effects_json": choose("side_effects", []),
            "failure_modes_json": choose("failure_modes", []),
            "counters_json": choose("counters", []),
            "source_claim_ids_json": self._claims(event)
            or deepcopy((existing or {}).get("source_claim_ids_json") or []),
            "source_event_id": source_event_id,
            "updated_order": int(updated_order),
        }
        self.modules[module_id] = row

    def _apply_slot(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str | None,
        updated_order: int,
    ) -> None:
        advantage_id = _required_text(
            event.get("advantage_id"),
            field="advantage_id",
        )
        self._definition(advantage_id)
        definition = _as_object(event.get("definition"), field="definition")
        slot_id = _required_text(
            event.get("slot_id") or event.get("spec_id"),
            field="slot_id",
        )
        module_id = event.get("module_id") or definition.get("module_id")
        if module_id is not None:
            self._module(advantage_id, str(module_id))
        capacity_raw = (
            event.get("capacity")
            if event.get("capacity") is not None
            else definition.get("capacity")
        )
        capacity = (
            _finite_number(
                capacity_raw,
                field="slot.capacity",
                minimum=0,
            )
            if capacity_raw is not None
            else None
        )
        authority_status = str(
            event.get("status")
            or event.get("authority_status")
            or definition.get("status")
            or definition.get("authority_status")
            or (
                "planned"
                if str(event.get("scope") or "") in {"planned", "author_plan"}
                else "canon"
            )
        )
        if authority_status not in ADVANTAGE_STATUSES:
            raise ContinuityError(
                "ADVANTAGE_STATUS_INVALID",
                f"unsupported slot authority status: {authority_status}",
            )
        self.slots[slot_id] = {
            "slot_id": slot_id,
            "advantage_id": advantage_id,
            "module_id": str(module_id) if module_id is not None else None,
            "stage": _required_text(
                event.get("stage")
                or definition.get("stage")
                or "initial",
                field="slot.stage",
            ),
            "capacity": capacity,
            "unlock_graph_json": deepcopy(
                event.get("unlock_graph")
                if event.get("unlock_graph") is not None
                else definition.get("unlock_graph")
                or {}
            ),
            "set_membership_json": _string_list(
                event.get("set_membership")
                if event.get("set_membership") is not None
                else definition.get("set_membership")
                or [],
                field="slot.set_membership",
            ),
            "authority_status": authority_status,
            "slot_status": str(
                event.get("slot_status")
                or definition.get("slot_status")
                or "locked"
            ),
            "source_claim_ids_json": self._claims(event),
            "source_event_id": source_event_id,
            "updated_order": int(updated_order),
        }

    def _apply_bind(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str | None,
        updated_order: int,
    ) -> None:
        advantage_id = _required_text(
            event.get("advantage_id"),
            field="advantage_id",
        )
        definition = self._definition(advantage_id)
        if str(definition["advantage_status"]) != "canon":
            raise ContinuityError(
                "ADVANTAGE_NONCANON_AUTHORITY",
                "planned, rumor or misread advantages cannot bind runtime",
                details={
                    "advantage_id": advantage_id,
                    "status": definition["advantage_status"],
                },
            )
        anchor_id = _required_text(
            event.get("anchor_id"),
            field="anchor_id",
        )
        anchor = self.anchors.get(anchor_id)
        if anchor is None or str(anchor["advantage_id"]) != advantage_id:
            raise ContinuityError(
                "ADVANTAGE_ANCHOR_NOT_FOUND",
                f"unknown anchor for advantage: {anchor_id}",
            )
        if str(anchor.get("authority_status") or "canon") != "canon":
            raise ContinuityError(
                "ADVANTAGE_NONCANON_AUTHORITY",
                "non-canon anchor cannot bind runtime",
                details={
                    "anchor_id": anchor_id,
                    "status": anchor.get("authority_status"),
                },
            )
        action = str(event.get("action") or "bind")
        binding_states = {
            "bind": "bound",
            "unbind": "unbound",
            "release": "released",
            "seal": "sealed",
            "contest": "contested",
        }
        if action not in binding_states:
            raise ContinuityError(
                "ADVANTAGE_ACTION_INVALID",
                f"unsupported advantage bind action: {action}",
            )
        owner = (
            event.get("owner_entity_id")
            if "owner_entity_id" in event
            else anchor.get("owner_entity_id")
        )
        branch_id = self._branch(event)
        if branch_id == "main":
            anchor["binding_state"] = binding_states[action]
            anchor["owner_entity_id"] = owner
            anchor["story_coordinate_json"] = dict(
                self._event_coordinate(event) or {}
            )
            anchor["source_event_id"] = source_event_id
            anchor["updated_order"] = int(updated_order)
        runtime = self._ensure_runtime(
            advantage_id,
            branch_id=branch_id,
            source_event_id=source_event_id,
            updated_order=updated_order,
            coordinate=self._event_coordinate(event),
        )
        runtime["owner_entity_id"] = owner if action == "bind" else None
        if action != "bind":
            runtime["enabled"] = 0
        runtime["source_event_id"] = source_event_id
        runtime["story_coordinate_json"] = dict(
            self._event_coordinate(event) or {}
        )
        runtime["updated_order"] = int(updated_order)
        self._record_ledger(
            event,
            advantage_id=advantage_id,
            module_id=None,
            entry_kind="bind",
            source_event_id=source_event_id,
            updated_order=updated_order,
            input_value={"action": action, "anchor_id": anchor_id},
            output_value={
                "binding_state": binding_states[action],
                "owner_entity_id": owner,
            },
        )

    def _apply_activate(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str | None,
        updated_order: int,
    ) -> None:
        advantage_id = _required_text(
            event.get("advantage_id"),
            field="advantage_id",
        )
        definition = self._definition(advantage_id)
        if str(definition["advantage_status"]) != "canon":
            raise ContinuityError(
                "ADVANTAGE_NONCANON_AUTHORITY",
                "non-canon advantage cannot become active runtime",
            )
        branch_id = self._branch(event)
        runtime = self._ensure_runtime(
            advantage_id,
            branch_id=branch_id,
            source_event_id=source_event_id,
            updated_order=updated_order,
            coordinate=self._event_coordinate(event),
        )
        action = str(event.get("action") or "activate")
        if action not in {"activate", "deactivate", "seal", "unseal"}:
            raise ContinuityError(
                "ADVANTAGE_ACTION_INVALID",
                f"unsupported activation action: {action}",
            )
        enabled = action in {"activate", "unseal"}
        if enabled and not runtime.get("owner_entity_id"):
            owner = event.get("owner_entity_id")
            if owner is None:
                raise ContinuityError(
                    "ADVANTAGE_OWNER_REQUIRED",
                    "activation requires a bound owner",
                )
            runtime["owner_entity_id"] = str(owner)
        runtime["enabled"] = int(enabled)
        runtime["stage"] = str(
            event.get("stage")
            or runtime.get("stage")
            or ("active" if enabled else "dormant")
        )
        # Bootstrap activation carries the exact runtime snapshot produced by
        # the initialization engine.  Apply those fields through the same
        # deterministic reducer instead of consulting the JSON sidecar during
        # replay.  Every numeric value is normalized and bounded here so a
        # malformed proposal cannot create negative/NaN state.
        if event.get("max_charges") is not None:
            runtime["max_charges"] = _finite_number(
                event["max_charges"],
                field="max_charges",
                minimum=0,
            )
        if event.get("resources") is not None:
            raw_resources = event.get("resources")
            if not isinstance(raw_resources, Mapping):
                raise ContinuityError(
                    "ADVANTAGE_INVALID_RESOURCE_DELTA",
                    "resources must be an object",
                )
            runtime["resources_json"] = {
                str(key): _finite_number(
                    value,
                    field=f"resources.{key}",
                    minimum=0,
                )
                for key, value in raw_resources.items()
                if str(key).strip()
            }
        for field_name in ("pollution", "exposure", "debt"):
            if event.get(field_name) is not None:
                runtime[field_name] = _finite_number(
                    event[field_name],
                    field=field_name,
                    minimum=0,
                )
        if "cooldown_until" in event:
            cooldown_until = event.get("cooldown_until")
            runtime["cooldown_until_json"] = (
                self._cooldown_until(
                    self._event_coordinate(event),
                    cooldown_until,
                )
                if cooldown_until is not None
                else None
            )
        if event.get("runtime_metadata") is not None:
            runtime["runtime_json"] = deepcopy(
                _as_object(
                    event.get("runtime_metadata"),
                    field="runtime_metadata",
                )
            )
        if event.get("charges") is not None:
            charges = _finite_number(
                event["charges"],
                field="charges",
                minimum=0,
            )
            maximum = runtime.get("max_charges")
            if maximum is not None and charges > float(maximum) + _EPSILON:
                raise ContinuityError(
                    "ADVANTAGE_CONSERVATION_VIOLATION",
                    "charges exceed max_charges",
                )
            runtime["charges"] = charges
        if (
            runtime.get("charges") is not None
            and runtime.get("max_charges") is not None
            and float(runtime["charges"])
            > float(runtime["max_charges"]) + _EPSILON
        ):
            raise ContinuityError(
                "ADVANTAGE_CONSERVATION_VIOLATION",
                "charges exceed max_charges",
            )
        runtime["source_event_id"] = source_event_id
        runtime["story_coordinate_json"] = dict(
            self._event_coordinate(event) or {}
        )
        runtime["updated_order"] = int(updated_order)
        self._record_ledger(
            event,
            advantage_id=advantage_id,
            module_id=None,
            entry_kind="activate",
            source_event_id=source_event_id,
            updated_order=updated_order,
            input_value={"action": action},
            output_value={
                "enabled": enabled,
                "stage": runtime["stage"],
            },
        )

    @staticmethod
    def _resource_amounts(
        values: Any,
        *,
        field: str,
    ) -> dict[str, float]:
        result: dict[str, float] = {}
        if values is None:
            return result
        if isinstance(values, Mapping):
            iterable = [
                {"resource": key, "amount": amount}
                for key, amount in values.items()
            ]
        elif isinstance(values, list):
            iterable = values
        else:
            raise ContinuityError(
                "ADVANTAGE_INVALID_RESOURCE_DELTA",
                f"{field} must be an object or array",
            )
        for raw in iterable:
            if not isinstance(raw, Mapping):
                continue
            kind = str(
                raw.get("resource")
                or raw.get("resource_id")
                or raw.get("kind")
                or ""
            ).strip()
            if not kind or kind in {
                "charge",
                "charges",
                "pollution",
                "exposure",
                "debt",
            }:
                continue
            amount = _finite_number(
                raw.get("amount", 0),
                field=f"{field}.{kind}",
                minimum=0,
            )
            result[kind] = result.get(kind, 0.0) + amount
        return result

    @staticmethod
    def _special_amount(
        values: Any,
        names: set[str],
        *,
        field: str,
    ) -> float:
        if isinstance(values, Mapping):
            iterable: Iterable[Any] = (
                {"resource": key, "amount": amount}
                for key, amount in values.items()
            )
        elif isinstance(values, list):
            iterable = values
        else:
            return 0.0
        total = 0.0
        for raw in iterable:
            if not isinstance(raw, Mapping):
                continue
            kind = str(
                raw.get("resource")
                or raw.get("resource_id")
                or raw.get("kind")
                or ""
            ).strip()
            if kind not in names:
                continue
            total += _finite_number(
                raw.get("amount", 0),
                field=field,
                minimum=0,
            )
        return total

    @staticmethod
    def _cooldown_until(
        coordinate: Mapping[str, Any] | None,
        cooldown: Any,
    ) -> dict[str, Any] | None:
        if cooldown is None:
            return None
        if isinstance(cooldown, Mapping):
            parsed = _coordinate(cooldown)
            if parsed is None:
                raise ContinuityError(
                    "ADVANTAGE_STORY_COORDINATE_INVALID",
                    "cooldown_until must be a story coordinate",
                )
            return parsed
        if coordinate is None:
            raise ContinuityError(
                "ADVANTAGE_STORY_COORDINATE_REQUIRED",
                "relative cooldown requires story_coordinate",
            )
        delta = _finite_number(
            cooldown,
            field="cooldown",
            minimum=0,
        )
        if int(delta) != delta:
            raise ContinuityError(
                "ADVANTAGE_COOLDOWN_INVALID",
                "cooldown must be a whole coordinate delta",
            )
        return {
            "calendar_id": str(coordinate["calendar_id"]),
            "ordinal": int(coordinate["ordinal"]) + int(delta),
        }

    @staticmethod
    def _assert_cooldown_ready(
        runtime: Mapping[str, Any],
        coordinate: Mapping[str, Any] | None,
    ) -> None:
        until = runtime.get("cooldown_until_json")
        if not until:
            return
        if coordinate is None:
            raise ContinuityError(
                "ADVANTAGE_STORY_COORDINATE_REQUIRED",
                "cooldown check requires story_coordinate",
            )
        if str(until.get("calendar_id")) != str(
            coordinate.get("calendar_id")
        ):
            raise ContinuityError(
                "ADVANTAGE_STORY_COORDINATE_CONFLICT",
                "runtime cooldown and event use different calendars",
            )
        if int(coordinate["ordinal"]) < int(until["ordinal"]):
            raise ContinuityError(
                "ADVANTAGE_COOLDOWN_ACTIVE",
                "advantage module is still on cooldown",
                details={
                    "current": dict(coordinate),
                    "cooldown_until": dict(until),
                },
            )

    def _record_ledger(
        self,
        event: Mapping[str, Any],
        *,
        advantage_id: str,
        module_id: str | None,
        entry_kind: str,
        source_event_id: str | None,
        updated_order: int,
        input_value: Any,
        output_value: Any,
        loss_value: Any | None = None,
    ) -> dict[str, Any]:
        branch_id = self._branch(event)
        entry_id = str(event.get("entry_id") or "").strip()
        if not entry_id:
            entry_id = stable_advantage_id(
                "advantage_ledger_",
                {
                    "source_event_id": source_event_id,
                    "updated_order": int(updated_order),
                    "advantage_id": advantage_id,
                    "module_id": module_id,
                    "entry_kind": entry_kind,
                    "branch_id": branch_id,
                },
            )
        entry_kind = str(entry_kind).strip()
        if not entry_kind or len(entry_kind) > 256:
            raise ContinuityError(
                "ADVANTAGE_LEDGER_KIND_INVALID",
                "ledger entry_kind must be 1..256 characters",
            )
        provenance = deepcopy(
            event.get("causal_provenance")
            or event.get("provenance")
            or {}
        )
        if event.get("experience_contract") is not None:
            provenance["experience_contract"] = deepcopy(
                event["experience_contract"]
            )
        if event.get("experience_contract_id") is not None:
            provenance["experience_contract_id"] = str(
                event["experience_contract_id"]
            )
        row = {
            "entry_id": entry_id,
            "advantage_id": advantage_id,
            "module_id": module_id,
            "branch_id": branch_id,
            "entry_kind": entry_kind,
            "actor_entity_id": event.get("actor_entity_id")
            or event.get("actor"),
            "target_entity_id": event.get("target_entity_id")
            or event.get("target"),
            "input_json": deepcopy(input_value),
            "output_json": deepcopy(output_value),
            "loss_json": deepcopy(loss_value or {}),
            "provenance_json": provenance,
            "causal_event_id": event.get("causal_event_id")
            or event.get("caused_by"),
            "source_event_id": source_event_id,
            "story_coordinate_json": dict(
                self._event_coordinate(event) or {}
            ),
            "updated_order": int(updated_order),
        }
        self.ledger[entry_id] = row
        return row

    def _apply_runtime_action(
        self,
        event: Mapping[str, Any],
        *,
        event_type: str,
        source_event_id: str | None,
        updated_order: int,
    ) -> None:
        self._assert_not_computed(event)
        advantage_id = _required_text(
            event.get("advantage_id"),
            field="advantage_id",
        )
        self._definition(advantage_id)
        # Historical sidecar ledger rows are replayed as record-only events.
        # They must remain auditable while leaving the already-materialized
        # runtime snapshot untouched (otherwise bootstrap costs/rewards would
        # be applied twice).  Keep the original stable entry id and kind.
        if bool(event.get("record_only")):
            module_id = str(event.get("module_id") or "").strip() or None
            if module_id is not None:
                self._module(advantage_id, module_id)
            entry_kind = str(
                event.get("ledger_entry_kind")
                or (
                    "cost"
                    if event_type == "advantage_cost"
                    else "reward"
                )
            ).strip()
            if not entry_kind:
                raise ContinuityError(
                    "ADVANTAGE_LEDGER_KIND_INVALID",
                    "record-only ledger entry requires ledger_entry_kind",
                )
            self._record_ledger(
                event,
                advantage_id=advantage_id,
                module_id=module_id,
                entry_kind=entry_kind,
                source_event_id=source_event_id,
                updated_order=updated_order,
                input_value=deepcopy(event.get("input") or {}),
                output_value=deepcopy(event.get("output") or {}),
                loss_value=deepcopy(event.get("loss") or {}),
            )
            return
        branch_id = self._branch(event)
        coordinate = self._event_coordinate(event)
        runtime = self._ensure_runtime(
            advantage_id,
            branch_id=branch_id,
            source_event_id=source_event_id,
            updated_order=updated_order,
            coordinate=coordinate,
        )
        if not bool(runtime.get("enabled")):
            raise ContinuityError(
                "ADVANTAGE_NOT_ACTIVE",
                "advantage runtime is not active",
                details={"advantage_id": advantage_id, "branch_id": branch_id},
            )
        module_id = (
            _required_text(event.get("module_id"), field="module_id")
            if event_type in {
                "advantage_trigger",
                "advantage_use",
            }
            else str(event.get("module_id") or "").strip() or None
        )
        module: dict[str, Any] | None = None
        if module_id is not None:
            module = self._module(
                advantage_id,
                module_id,
                usable=event_type in {"advantage_trigger", "advantage_use"},
            )
            unlocked = set(runtime.get("unlocked_modules_json") or [])
            if (
                event_type in {"advantage_trigger", "advantage_use"}
                and module_id not in unlocked
                and str(module.get("module_status")) != "enabled"
            ):
                raise ContinuityError(
                    "ADVANTAGE_MODULE_LOCKED",
                    "advantage runtime has not unlocked this module",
                    details={"module_id": module_id},
                )
        if event_type in {"advantage_trigger", "advantage_use"}:
            self._assert_cooldown_ready(runtime, coordinate)

        module_costs = (
            deepcopy(module.get("costs_json") or []) if module else []
        )
        costs = (
            deepcopy(event.get("costs"))
            if "costs" in event
            else module_costs
        )
        rewards = (
            deepcopy(event.get("rewards"))
            if "rewards" in event
            else []
        )
        declared_output = (
            deepcopy(event.get("output"))
            if "output" in event
            else {}
        )

        resources = {
            str(key): _finite_number(
                value,
                field=f"resources.{key}",
                minimum=0,
            )
            for key, value in dict(runtime.get("resources_json") or {}).items()
        }
        cost_resources = self._resource_amounts(costs, field="costs")
        reward_resources = self._resource_amounts(rewards, field="rewards")
        before_resources = dict(resources)
        for key, amount in cost_resources.items():
            current = resources.get(key, 0.0)
            if current + _EPSILON < amount:
                raise ContinuityError(
                    "ADVANTAGE_RESOURCE_INSUFFICIENT",
                    f"insufficient advantage resource: {key}",
                    details={
                        "resource": key,
                        "available": current,
                        "required": amount,
                    },
                )
            resources[key] = max(0.0, current - amount)
        for key, amount in reward_resources.items():
            resources[key] = resources.get(key, 0.0) + amount

        charge_cost = self._special_amount(
            costs,
            {"charge", "charges"},
            field="costs.charges",
        )
        charge_reward = self._special_amount(
            rewards,
            {"charge", "charges"},
            field="rewards.charges",
        )
        charges = runtime.get("charges")
        if charge_cost or charge_reward:
            current_charges = float(charges or 0.0)
            if current_charges + _EPSILON < charge_cost:
                raise ContinuityError(
                    "ADVANTAGE_CHARGES_INSUFFICIENT",
                    "advantage has insufficient charges",
                )
            current_charges = current_charges - charge_cost + charge_reward
            maximum = runtime.get("max_charges")
            if maximum is not None:
                current_charges = min(current_charges, float(maximum))
            runtime["charges"] = max(0.0, current_charges)

        def scalar_delta(
            name: str,
            *,
            costs_add: bool,
            event_key: str,
        ) -> float:
            cost_amount = self._special_amount(
                costs,
                {name},
                field=f"costs.{name}",
            )
            reward_amount = self._special_amount(
                rewards,
                {name},
                field=f"rewards.{name}",
            )
            direct = _finite_number(
                event.get(event_key, 0),
                field=event_key,
            )
            return direct + (
                cost_amount - reward_amount
                if costs_add
                else reward_amount - cost_amount
            )

        pollution_delta = scalar_delta(
            "pollution",
            costs_add=True,
            event_key="pollution_delta",
        )
        exposure_delta = scalar_delta(
            "exposure",
            costs_add=True,
            event_key="exposure_delta",
        )
        debt_delta = scalar_delta(
            "debt",
            costs_add=True,
            event_key="debt_delta",
        )
        runtime["resources_json"] = resources
        runtime["pollution"] = _finite_number(
            float(runtime.get("pollution") or 0) + pollution_delta,
            field="pollution",
            minimum=0,
        )
        runtime["exposure"] = _finite_number(
            float(runtime.get("exposure") or 0) + exposure_delta,
            field="exposure",
            minimum=0,
        )
        runtime["debt"] = _finite_number(
            float(runtime.get("debt") or 0) + debt_delta,
            field="debt",
            minimum=0,
        )
        cooldown = (
            event.get("cooldown")
            if event.get("cooldown") is not None
            else (
                _as_object(module.get("trigger_json"), field="trigger")
                .get("cooldown")
                if module
                else None
            )
        )
        if event_type in {"advantage_trigger", "advantage_use"} and cooldown:
            runtime["cooldown_until_json"] = self._cooldown_until(
                coordinate,
                cooldown,
            )
        runtime["source_event_id"] = source_event_id
        runtime["story_coordinate_json"] = dict(coordinate or {})
        runtime["updated_order"] = int(updated_order)

        kinds = {
            "advantage_trigger": "trigger",
            "advantage_use": "use",
            "advantage_reward": "reward",
            "advantage_cost": "cost",
        }
        self._record_ledger(
            event,
            advantage_id=advantage_id,
            module_id=module_id,
            entry_kind=kinds[event_type],
            source_event_id=source_event_id,
            updated_order=updated_order,
            input_value={
                "costs": costs,
                "resources_before": before_resources,
            },
            output_value={
                "effects": deepcopy(
                    event.get("effects")
                    or (module or {}).get("effects_json")
                    or []
                ),
                "output": declared_output,
                "rewards": rewards,
                "resources_after": resources,
                "charges": runtime.get("charges"),
            },
            loss_value={
                "side_effects": deepcopy(
                    event.get("side_effects")
                    or (module or {}).get("side_effects_json")
                    or []
                ),
                "pollution_delta": pollution_delta,
                "exposure_delta": exposure_delta,
                "debt_delta": debt_delta,
            },
        )

    def _apply_upgrade(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str | None,
        updated_order: int,
    ) -> None:
        self._assert_not_computed(event)
        advantage_id = _required_text(
            event.get("advantage_id"),
            field="advantage_id",
        )
        branch_id = self._branch(event)
        runtime = self._ensure_runtime(
            advantage_id,
            branch_id=branch_id,
            source_event_id=source_event_id,
            updated_order=updated_order,
            coordinate=self._event_coordinate(event),
        )
        if not bool(runtime.get("enabled")):
            raise ContinuityError(
                "ADVANTAGE_NOT_ACTIVE",
                "advantage runtime must be active before upgrade",
                details={
                    "advantage_id": advantage_id,
                    "branch_id": branch_id,
                },
            )
        from_stage = str(runtime.get("stage") or "")
        to_stage = _required_text(
            event.get("to_stage") or event.get("stage"),
            field="to_stage",
        )
        unlock_modules = _string_list(
            event.get("unlock_modules") or [],
            field="unlock_modules",
        )
        for module_id in unlock_modules:
            module = self._module(advantage_id, module_id)
            if str(module.get("authority_status") or "canon") != "canon":
                raise ContinuityError(
                    "ADVANTAGE_NONCANON_AUTHORITY",
                    "upgrade cannot unlock a non-canon module",
                    details={
                        "module_id": module_id,
                        "status": module.get("authority_status"),
                    },
                )
            if branch_id == "main":
                module["module_status"] = "available"
                module["source_event_id"] = source_event_id
                module["updated_order"] = int(updated_order)
        unlocked = set(runtime.get("unlocked_modules_json") or [])
        unlocked.update(unlock_modules)
        runtime["unlocked_modules_json"] = sorted(unlocked)
        runtime["stage"] = to_stage
        if event.get("max_charges") is not None:
            maximum = _finite_number(
                event["max_charges"],
                field="max_charges",
                minimum=0,
            )
            runtime["max_charges"] = maximum
            if runtime.get("charges") is not None:
                runtime["charges"] = min(
                    float(runtime["charges"]),
                    maximum,
                )
        runtime["source_event_id"] = source_event_id
        runtime["story_coordinate_json"] = dict(
            self._event_coordinate(event) or {}
        )
        runtime["updated_order"] = int(updated_order)
        self._record_ledger(
            event,
            advantage_id=advantage_id,
            module_id=None,
            entry_kind="upgrade",
            source_event_id=source_event_id,
            updated_order=updated_order,
            input_value={"from_stage": from_stage},
            output_value={
                "to_stage": to_stage,
                "unlock_modules": unlock_modules,
            },
        )

    def _apply_reveal(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str | None,
        updated_order: int,
    ) -> None:
        advantage_id = _required_text(
            event.get("advantage_id"),
            field="advantage_id",
        )
        self._definition(advantage_id)
        module_id = str(event.get("module_id") or "").strip() or None
        if module_id is not None:
            self._module(advantage_id, module_id)
        plane = str(event.get("knowledge_plane") or "")
        if plane not in ADVANTAGE_KNOWLEDGE_PLANES:
            raise ContinuityError(
                "ADVANTAGE_KNOWLEDGE_PLANE_INVALID",
                f"unsupported knowledge plane: {plane}",
            )
        status = str(event.get("status") or "canon")
        if status not in ADVANTAGE_STATUSES:
            raise ContinuityError(
                "ADVANTAGE_STATUS_INVALID",
                f"unsupported knowledge status: {status}",
            )
        knowledge_id = str(event.get("knowledge_id") or "").strip()
        if not knowledge_id:
            knowledge_id = stable_advantage_id(
                "advantage_knowledge_",
                {
                    "advantage_id": advantage_id,
                    "module_id": module_id,
                    "observer_entity_id": event.get("observer_entity_id"),
                    "knowledge_plane": plane,
                    "claim": event.get("claim"),
                    "reveal_stage": event.get("reveal_stage"),
                },
            )
        misread_of = str(event.get("misread_of") or "").strip() or None
        if status == "misread" and not misread_of:
            raise ContinuityError(
                "ADVANTAGE_MISREAD_REFERENCE_REQUIRED",
                "misread knowledge must identify the claim it misreads",
            )
        confidence = _finite_number(
            event.get("confidence", 1.0),
            field="confidence",
            minimum=0,
            maximum=1,
        )
        self.knowledge[knowledge_id] = {
            "knowledge_id": knowledge_id,
            "advantage_id": advantage_id,
            "module_id": module_id,
            "observer_entity_id": event.get("observer_entity_id"),
            "knowledge_plane": plane,
            "knowledge_status": status,
            "claim_json": deepcopy(event.get("claim") or {}),
            "confidence": confidence,
            "evidence_json": deepcopy(event.get("evidence") or {}),
            "reveal_stage": _required_text(
                event.get("reveal_stage") or "current",
                field="reveal_stage",
            ),
            "misread_of": misread_of,
            "source_claim_ids_json": self._claims(event),
            "source_event_id": source_event_id,
            "story_coordinate_json": dict(
                self._event_coordinate(event) or {}
            ),
            "updated_order": int(updated_order),
        }
        if status == "canon" and bool(event.get("record_ledger", True)):
            self._record_ledger(
                event,
                advantage_id=advantage_id,
                module_id=module_id,
                entry_kind="reveal",
                source_event_id=source_event_id,
                updated_order=updated_order,
                input_value={"knowledge_plane": plane},
                output_value={
                    "knowledge_id": knowledge_id,
                    "status": status,
                    "reveal_stage": event.get("reveal_stage") or "current",
                },
            )

    def _apply_contract(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str | None,
        updated_order: int,
    ) -> None:
        advantage_id = _required_text(
            event.get("advantage_id"),
            field="advantage_id",
        )
        self._definition(advantage_id)
        contract_id = _required_text(
            event.get("contract_id"),
            field="contract_id",
        )
        action = str(event.get("action") or "define")
        if action == "narrative":
            self._apply_narrative_contract(
                event,
                source_event_id=source_event_id,
                updated_order=updated_order,
            )
            return
        existing = self.contracts.get(contract_id)
        authority_status = str(
            event.get("status")
            or event.get("authority_status")
            or (existing or {}).get("authority_status")
            or "canon"
        )
        if authority_status not in ADVANTAGE_STATUSES:
            raise ContinuityError(
                "ADVANTAGE_STATUS_INVALID",
                f"unsupported contract authority status: {authority_status}",
            )
        statuses = {
            "define": str(event.get("contract_status") or "proposed"),
            "activate": "active",
            "suspend": "suspended",
            "breach": "breached",
            "fulfill": "fulfilled",
            "terminate": "terminated",
            "update": str(
                event.get("contract_status")
                or (existing or {}).get("contract_status")
                or "proposed"
            ),
        }
        if action not in statuses:
            raise ContinuityError(
                "ADVANTAGE_ACTION_INVALID",
                f"unsupported contract action: {action}",
            )
        if action not in {"define"} and existing is None:
            raise ContinuityError(
                "ADVANTAGE_CONTRACT_NOT_FOUND",
                f"unknown advantage contract: {contract_id}",
            )
        trust_delta = _finite_number(
            event.get("trust_delta", 0),
            field="trust_delta",
        )
        debt_delta = _finite_number(
            event.get("debt_delta", 0),
            field="debt_delta",
        )
        trust = float((existing or {}).get("trust") or 0) + trust_delta
        debt = _finite_number(
            float((existing or {}).get("debt") or 0) + debt_delta,
            field="contract.debt",
            minimum=0,
        )
        row = {
            "contract_id": contract_id,
            "advantage_id": advantage_id,
            "actor_entity_id": event.get("actor_entity_id")
            if "actor_entity_id" in event
            else (existing or {}).get("actor_entity_id"),
            "counterparty_entity_id": event.get("counterparty_entity_id")
            if "counterparty_entity_id" in event
            else (existing or {}).get("counterparty_entity_id"),
            "authority_status": authority_status,
            "contract_status": statuses[action],
            "terms_json": deepcopy(
                event.get("terms")
                if event.get("terms") is not None
                else (existing or {}).get("terms_json")
                or []
            ),
            "agency_json": deepcopy(
                event.get("agency")
                if event.get("agency") is not None
                else (existing or {}).get("agency_json")
                or {}
            ),
            "trust": trust,
            "debt": debt,
            "breach_effect_json": deepcopy(
                event.get("breach_effect")
                if event.get("breach_effect") is not None
                else (existing or {}).get("breach_effect_json")
                or {}
            ),
            "source_claim_ids_json": self._claims(event)
            or deepcopy((existing or {}).get("source_claim_ids_json") or []),
            "source_event_id": source_event_id,
            "story_coordinate_json": dict(
                self._event_coordinate(event)
                or (existing or {}).get("story_coordinate_json")
                or {}
            ),
            "updated_order": int(updated_order),
        }
        self.contracts[contract_id] = row
        if authority_status != "canon":
            return
        branch_id = self._branch(event)
        runtime = self._ensure_runtime(
            advantage_id,
            branch_id=branch_id,
            source_event_id=source_event_id,
            updated_order=updated_order,
            coordinate=self._event_coordinate(event),
        )
        runtime["debt"] = _finite_number(
            sum(
                float(contract.get("debt") or 0)
                for contract in self.contracts.values()
                if contract.get("advantage_id") == advantage_id
                and contract.get("contract_status")
                in {"proposed", "active", "suspended", "breached"}
            ),
            field="runtime.debt",
            minimum=0,
        )
        runtime["source_event_id"] = source_event_id
        runtime["updated_order"] = int(updated_order)
        self._record_ledger(
            event,
            advantage_id=advantage_id,
            module_id=None,
            entry_kind="breach" if action == "breach" else "contract",
            source_event_id=source_event_id,
            updated_order=updated_order,
            input_value={
                "action": action,
                "trust_delta": trust_delta,
                "debt_delta": debt_delta,
            },
            output_value={
                "contract_id": contract_id,
                "contract_status": statuses[action],
                "trust": trust,
                "debt": debt,
            },
        )

    def _apply_narrative_contract(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str | None,
        updated_order: int,
    ) -> None:
        advantage_id = _required_text(
            event.get("advantage_id"),
            field="advantage_id",
        )
        self._definition(advantage_id)
        definition = _as_object(event.get("definition"), field="definition")
        narrative_contract_id = _required_text(
            event.get("narrative_contract_id")
            or event.get("contract_id")
            or event.get("spec_id"),
            field="narrative_contract_id",
        )
        authority_status = str(
            event.get("status")
            or event.get("authority_status")
            or definition.get("status")
            or definition.get("authority_status")
            or "canon"
        )
        if authority_status not in ADVANTAGE_STATUSES:
            raise ContinuityError(
                "ADVANTAGE_STATUS_INVALID",
                "unsupported narrative contract authority status: "
                f"{authority_status}",
            )
        status = str(
            event.get("contract_status")
            or definition.get("contract_status")
            or ("active" if authority_status == "canon" else "planned")
        )
        if status not in {"active", "planned", "retired"}:
            raise ContinuityError(
                "ADVANTAGE_NARRATIVE_CONTRACT_STATUS_INVALID",
                f"unsupported narrative contract status: {status}",
            )
        existing = next(
            (
                value
                for value in self.narrative_contracts.values()
                if value.get("advantage_id") == advantage_id
            ),
            None,
        )
        if (
            existing is not None
            and str(existing["narrative_contract_id"])
            != narrative_contract_id
        ):
            del self.narrative_contracts[
                str(existing["narrative_contract_id"])
            ]
        self.narrative_contracts[narrative_contract_id] = {
            "narrative_contract_id": narrative_contract_id,
            "advantage_id": advantage_id,
            "authority_status": authority_status,
            "contract_status": status,
            "reading_promise_json": deepcopy(
                event.get("reading_promise")
                if event.get("reading_promise") is not None
                else definition.get("reading_promise")
                or {}
            ),
            "reward_loop_json": deepcopy(
                event.get("reward_loop")
                if event.get("reward_loop") is not None
                else definition.get("reward_loop")
                or []
            ),
            "risk_loop_json": deepcopy(
                event.get("risk_loop")
                if event.get("risk_loop") is not None
                else definition.get("risk_loop")
                or []
            ),
            "reveal_ladder_json": deepcopy(
                event.get("reveal_ladder")
                if event.get("reveal_ladder") is not None
                else definition.get("reveal_ladder")
                or []
            ),
            "experience_binding_json": deepcopy(
                event.get("experience_binding")
                if event.get("experience_binding") is not None
                else definition.get("experience_binding")
                or {}
            ),
            "source_claim_ids_json": self._claims(event),
            "source_event_id": source_event_id,
            "updated_order": int(updated_order),
        }

    def apply(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str | None,
        updated_order: int,
    ) -> None:
        """Apply one normalized Advantage event."""

        event_type = str(event.get("event_type") or "")
        if event_type not in ADVANTAGE_EVENT_TYPES:
            raise ContinuityError(
                "ADVANTAGE_EVENT_TYPE_INVALID",
                f"unsupported advantage event type: {event_type}",
            )
        scope = str(event.get("scope") or "current")
        is_flashback = str(
            event.get("narrative_mode") or "linear"
        ) == "flashback"
        # Planned and historical events remain available through the generic
        # planned/history projections.  The specialized Advantage projection
        # represents current/timeless runtime only, so neither plane may
        # mutate it (including through a correction wrapper).
        if scope in {"planned", "historical"} or is_flashback:
            return
        branch_id = self._branch(event)
        if branch_id != "main":
            branch_local = event_type in ADVANTAGE_BRANCH_LOCAL_EVENT_TYPES
            if event_type == "advantage_module":
                branch_local = str(event.get("action") or "") in {
                    "unlock",
                    "enable",
                    "lock",
                    "suppress",
                    "deprecate",
                }
            if not branch_local:
                raise ContinuityError(
                    "ADVANTAGE_BRANCH_EVENT_UNSUPPORTED",
                    (
                        "non-main Advantage events may only mutate "
                        "branch-keyed runtime or ledger state"
                    ),
                    details={
                        "event_type": event_type,
                        "branch_id": branch_id,
                    },
                )
        handlers = {
            "advantage_spec": self._apply_spec,
            "advantage_anchor": self._apply_anchor,
            "advantage_module": self._apply_module,
            "advantage_bind": self._apply_bind,
            "advantage_activate": self._apply_activate,
            "advantage_upgrade": self._apply_upgrade,
            "advantage_reveal": self._apply_reveal,
            "advantage_contract": self._apply_contract,
        }
        if event_type in {
            "advantage_trigger",
            "advantage_use",
            "advantage_reward",
            "advantage_cost",
        }:
            self._apply_runtime_action(
                event,
                event_type=event_type,
                source_event_id=source_event_id,
                updated_order=updated_order,
            )
            return
        if event_type == "advantage_correction":
            # Normal event-row corrections are expanded by ``_event_payload``.
            # A direct correction with a replacement remains supported.
            replacement = event.get("replacement")
            if not isinstance(replacement, Mapping):
                if str(event.get("action") or "") == "retract":
                    return
                raise ContinuityError(
                    "ADVANTAGE_CORRECTION_INVALID",
                    "advantage correction requires replacement or retract",
                )
            self.apply(
                dict(replacement),
                source_event_id=source_event_id,
                updated_order=updated_order,
            )
            return
        handler = handlers[event_type]
        handler(
            event,
            source_event_id=source_event_id,
            updated_order=updated_order,
        )

    def apply_sequence(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        source_prefix: str = "advantage_event_",
        updated_order_start: int = 1,
    ) -> None:
        for index, event in enumerate(events):
            mapped = dict(event)
            self.apply(
                mapped,
                source_event_id=str(
                    mapped.get("source_event_id")
                    or f"{source_prefix}{index + 1}"
                ),
                updated_order=int(updated_order_start) + index,
            )


def validate_advantage_event_sequence(
    connection: sqlite3.Connection,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Dry-run a candidate event sequence against current accepted state."""

    state = AdvantageProjectionState.from_connection(connection)
    state.apply_sequence(
        events,
        source_prefix="validation_advantage_event_",
        updated_order_start=(
            _meta_int(connection, "head_canon_revision") + 1
        )
        * 1_000_000,
    )
    return {
        "schema_version": ADVANTAGE_SCHEMA_VERSION,
        "event_count": len(events),
        "status": "passed",
    }


def rebuild_advantage_projection(
    connection: sqlite3.Connection,
    event_rows: Sequence[Mapping[str, Any]],
    inactive_event_ids: set[str] | frozenset[str],
    *,
    bootstrap: Mapping[str, Any] | None = None,
    record_run: bool = True,
) -> dict[str, Any]:
    """Rebuild the independent Advantage projection from accepted events."""

    ensure_advantage_schema(connection)
    for table in _ADVANTAGE_TABLE_DELETE_ORDER:
        connection.execute(f'DELETE FROM "{table}"')
    state = AdvantageProjectionState()
    if bootstrap is not None:
        _apply_sidecar_to_state(state, bootstrap)
    event_count = 0
    latest_event_id: str | None = None
    latest_order = 0
    ordered_event_rows = sorted(
        (dict(raw_row) for raw_row in event_rows),
        key=lambda row: (
            int(row.get("updated_order") or 0),
            str(row.get("event_id") or ""),
        ),
    )
    for row in ordered_event_rows:
        event_id = str(row.get("event_id") or "")
        if event_id in inactive_event_ids:
            continue
        if (
            "changes_authority" in row
            and not bool(row["changes_authority"])
            and str(row.get("branch_id") or "main") == "main"
        ):
            continue
        event_type, payload = _event_payload(row)
        if event_type not in ADVANTAGE_EVENT_TYPES - {"advantage_correction"}:
            continue
        if not payload:
            continue
        payload["event_type"] = event_type
        latest_event_id = event_id or None
        latest_order = int(row.get("updated_order") or event_count + 1)
        state.apply(
            payload,
            source_event_id=latest_event_id,
            updated_order=latest_order,
        )
        event_count += 1
    state.persist(connection)
    projection_hash = refresh_advantage_projection_metadata(
        connection,
        source_event_id=latest_event_id,
        updated_order=latest_order,
    )
    run_id: str | None = None
    if record_run:
        try:
            connection.execute(
                "SELECT 1 FROM projection_runs LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            pass
        else:
            head = _meta_int(connection, "head_canon_revision")
            active = _meta_int(connection, "active_canon_revision")
            run_id = stable_advantage_id(
                "advantage_projection_run_",
                {
                    "head": head,
                    "active": active,
                    "projection_hash": projection_hash,
                },
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO projection_runs(
                    run_id, projection_name, source_head_revision,
                    source_active_revision, run_status, projection_hash,
                    details_json, created_at, completed_at
                ) VALUES(
                    ?, 'advantages', ?, ?, 'completed', ?, ?,
                    '1970-01-01T00:00:00+00:00',
                    '1970-01-01T00:00:00+00:00'
                )
                """,
                (
                    run_id,
                    head,
                    active,
                    projection_hash,
                    canonical_json(
                        {
                            "schema_version": (
                                ADVANTAGE_PROJECTION_SCHEMA_VERSION
                            ),
                            "event_count": event_count,
                            "inactive_event_count": len(inactive_event_ids),
                        }
                    ),
                ),
            )
    return {
        "advantage_projection_hash": projection_hash,
        "advantage_projection_schema_version": (
            ADVANTAGE_PROJECTION_SCHEMA_VERSION
        ),
        "advantage_event_count": event_count,
        "advantage_projection_run_id": run_id,
    }


def _sidecar_records(
    payload: Mapping[str, Any],
    name: str,
) -> list[dict[str, Any]]:
    raw = payload.get(name) or []
    if not isinstance(raw, list):
        raise ContinuityError(
            "ADVANTAGE_SIDECAR_INVALID",
            f"{name} must be an array",
        )
    return [dict(value) for value in raw if isinstance(value, Mapping)]


def _apply_sidecar_to_state(
    state: AdvantageProjectionState,
    sidecar: Mapping[str, Any],
) -> None:
    version = str(sidecar.get("schema_version") or "")
    if version != ADVANTAGE_SCHEMA_VERSION:
        raise ContinuityError(
            "ADVANTAGE_SIDECAR_SCHEMA_UNSUPPORTED",
            f"stored={version}, supported={ADVANTAGE_SCHEMA_VERSION}",
        )
    ordinal = 1

    def apply(event: dict[str, Any]) -> None:
        nonlocal ordinal
        event.setdefault("source_event_id", None)
        state.apply(
            event,
            source_event_id=None,
            updated_order=ordinal,
        )
        ordinal += 1

    for definition in _sidecar_records(sidecar, "definitions"):
        advantage_id = definition.get("advantage_id")
        apply(
            {
                **definition,
                "event_type": "advantage_spec",
                "action": "define",
                "spec_type": "advantage_definition",
                "definition": definition,
                "advantage_id": advantage_id,
            }
        )
    for anchor in _sidecar_records(sidecar, "anchors"):
        apply(
            {
                **anchor,
                "event_type": "advantage_anchor",
                "action": "define",
            }
        )
    for module in _sidecar_records(sidecar, "modules"):
        apply(
            {
                **module,
                "event_type": "advantage_module",
                "action": "define",
                "definition": module,
            }
        )
    for slot in _sidecar_records(sidecar, "runtime_slots"):
        apply(
            {
                **slot,
                "event_type": "advantage_spec",
                "action": "define",
                "spec_type": "runtime_slot",
                "definition": slot,
            }
        )
    for narrative in _sidecar_records(sidecar, "narrative_contracts"):
        apply(
            {
                **narrative,
                "event_type": "advantage_spec",
                "action": "define",
                "spec_type": "narrative_contract",
                "definition": narrative,
            }
        )
    for knowledge in _sidecar_records(sidecar, "knowledge"):
        apply(
            {
                **knowledge,
                "event_type": "advantage_reveal",
                "record_ledger": False,
            }
        )
    for contract in _sidecar_records(sidecar, "contracts"):
        apply(
            {
                **contract,
                "event_type": "advantage_contract",
                "action": "define",
            }
        )
    for runtime_raw in _sidecar_records(sidecar, "runtime_bootstrap"):
        advantage_id = _required_text(
            runtime_raw.get("advantage_id"),
            field="runtime_bootstrap.advantage_id",
        )
        branch_id = str(runtime_raw.get("branch_id") or "main")
        runtime = state._ensure_runtime(
            advantage_id,
            branch_id=branch_id,
            source_event_id=None,
            updated_order=ordinal,
            coordinate=_coordinate(runtime_raw.get("story_coordinate")),
        )
        definition = state._definition(advantage_id)
        if (
            bool(runtime_raw.get("enabled"))
            and str(definition["advantage_status"]) != "canon"
        ):
            raise ContinuityError(
                "ADVANTAGE_NONCANON_AUTHORITY",
                "non-canon sidecar runtime cannot be enabled",
            )
        runtime.update(
            {
                "owner_entity_id": runtime_raw.get("owner_entity_id"),
                "stage": str(runtime_raw.get("stage") or runtime["stage"]),
                "enabled": int(bool(runtime_raw.get("enabled", False))),
                "charges": (
                    _finite_number(
                        runtime_raw["charges"],
                        field="runtime_bootstrap.charges",
                        minimum=0,
                    )
                    if runtime_raw.get("charges") is not None
                    else runtime.get("charges")
                ),
                "max_charges": (
                    _finite_number(
                        runtime_raw["max_charges"],
                        field="runtime_bootstrap.max_charges",
                        minimum=0,
                    )
                    if runtime_raw.get("max_charges") is not None
                    else runtime.get("max_charges")
                ),
                "cooldown_until_json": _coordinate(
                    runtime_raw.get("cooldown_until")
                ),
                "resources_json": deepcopy(
                    runtime_raw.get("resources") or {}
                ),
                "pollution": _finite_number(
                    runtime_raw.get("pollution", 0),
                    field="runtime_bootstrap.pollution",
                    minimum=0,
                ),
                "exposure": _finite_number(
                    runtime_raw.get("exposure", 0),
                    field="runtime_bootstrap.exposure",
                    minimum=0,
                ),
                "debt": _finite_number(
                    runtime_raw.get("debt", 0),
                    field="runtime_bootstrap.debt",
                    minimum=0,
                ),
                "unlocked_modules_json": _string_list(
                    runtime_raw.get("unlocked_modules") or [],
                    field="runtime_bootstrap.unlocked_modules",
                ),
                "runtime_json": deepcopy(
                    runtime_raw.get("runtime")
                    or runtime_raw.get("runtime_metadata")
                    or {}
                ),
                "story_coordinate_json": dict(
                    _coordinate(runtime_raw.get("story_coordinate")) or {}
                ),
                "updated_order": ordinal,
            }
        )
        if (
            runtime.get("charges") is not None
            and runtime.get("max_charges") is not None
            and float(runtime["charges"]) > float(runtime["max_charges"])
        ):
            raise ContinuityError(
                "ADVANTAGE_CONSERVATION_VIOLATION",
                "bootstrap charges exceed max_charges",
            )
        ordinal += 1
    for ledger in _sidecar_records(sidecar, "ledger_bootstrap"):
        advantage_id = _required_text(
            ledger.get("advantage_id"),
            field="ledger_bootstrap.advantage_id",
        )
        state._definition(advantage_id)
        ledger_event = dict(ledger)
        entry_kind = _required_text(
            ledger.get("entry_kind") or "bootstrap",
            field="ledger_bootstrap.entry_kind",
        )
        if len(entry_kind) > 256:
            raise ContinuityError(
                "ADVANTAGE_LEDGER_KIND_INVALID",
                "ledger entry_kind exceeds 256 characters",
            )
        # Keep the historical provenance marker for readers of pre-v1
        # projections while preserving the domain entry_kind itself.
        provenance = dict(ledger_event.get("provenance") or {})
        provenance.setdefault("sidecar_entry_kind", entry_kind)
        ledger_event["provenance"] = provenance
        state._record_ledger(
            ledger_event,
            advantage_id=advantage_id,
            module_id=str(ledger.get("module_id") or "").strip() or None,
            entry_kind=entry_kind,
            source_event_id=None,
            updated_order=ordinal,
            input_value=ledger.get("input") or {},
            output_value=ledger.get("output") or {},
            loss_value=ledger.get("loss") or {},
        )
        ordinal += 1


def bootstrap_advantage_projection(
    connection: sqlite3.Connection,
    sidecar: Mapping[str, Any],
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Load an initialization sidecar through the same deterministic state."""

    ensure_advantage_schema(connection)
    state = (
        AdvantageProjectionState()
        if replace
        else AdvantageProjectionState.from_connection(connection)
    )
    _apply_sidecar_to_state(state, sidecar)
    state.persist(connection)
    projection_hash = refresh_advantage_projection_metadata(connection)
    return {
        "schema_version": ADVANTAGE_SCHEMA_VERSION,
        "advantage_projection_hash": projection_hash,
        "definition_count": len(state.definitions),
        "anchor_count": len(state.anchors),
        "module_count": len(state.modules),
    }


def load_advantage_sidecar(path_or_root: str | Path) -> dict[str, Any]:
    """Read ``advantages.v1.json`` from a file or project root."""

    path = Path(path_or_root).expanduser().resolve()
    if path.is_dir():
        path = path / ".plot-rag" / "advantages.v1.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContinuityError(
            "ADVANTAGE_SIDECAR_INVALID",
            "advantage sidecar cannot be read",
            details={"path": str(path)},
        ) from exc
    if not isinstance(payload, Mapping):
        raise ContinuityError(
            "ADVANTAGE_SIDECAR_INVALID",
            "advantage sidecar root must be an object",
            details={"path": str(path)},
        )
    return dict(payload)


def _query_rows(
    connection: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    if not advantage_schema_ready(connection):
        raise ContinuityError(
            "ADVANTAGE_SCHEMA_MISSING",
            "Advantage query requires a complete pre-existing schema",
        )
    cursor = connection.execute(sql, tuple(params))
    columns = [str(value[0]) for value in cursor.description or ()]
    return [
        _decoded_row(row, columns=columns)
        for row in cursor.fetchall()
    ]


def query_advantage_definition(
    connection: sqlite3.Connection,
    advantage_id: str,
) -> dict[str, Any] | None:
    rows = _query_rows(
        connection,
        """
        SELECT * FROM advantage_definitions
        WHERE advantage_id=?
        """,
        (advantage_id,),
    )
    return rows[0] if rows else None


def query_advantage_definitions(
    connection: sqlite3.Connection,
    *,
    owner_entity_id: str | None = None,
    status: str | None = None,
    profile: str | None = None,
    branch_id: str | None = None,
    generation_visible_only: bool = False,
) -> list[dict[str, Any]]:
    rows = _query_rows(
        connection,
        """
        SELECT DISTINCT d.*
        FROM advantage_definitions AS d
        LEFT JOIN advantage_anchors AS a
          ON a.advantage_id=d.advantage_id
         AND a.anchor_status='active'
        LEFT JOIN advantage_runtime_state AS r
          ON r.advantage_id=d.advantage_id
         AND (? IS NULL OR r.branch_id=?)
        WHERE (? IS NULL OR a.owner_entity_id=? OR r.owner_entity_id=?)
          AND (? IS NULL OR d.advantage_status=?)
          AND (
            ?=0
            OR (
              d.advantage_status='canon'
              AND d.lifecycle_status='active'
            )
          )
        ORDER BY d.advantage_id
        """,
        (
            branch_id,
            branch_id,
            owner_entity_id,
            owner_entity_id,
            owner_entity_id,
            status,
            status,
            int(generation_visible_only),
        ),
    )
    if profile is not None:
        rows = [
            row
            for row in rows
            if profile in set(row.get("profiles_json") or [])
        ]
    return rows


def query_advantage_anchors(
    connection: sqlite3.Connection,
    advantage_id: str,
    *,
    active_only: bool = True,
    include_noncanon: bool = False,
) -> list[dict[str, Any]]:
    return _query_rows(
        connection,
        """
        SELECT * FROM advantage_anchors
        WHERE advantage_id=?
          AND (?=0 OR anchor_status='active')
          AND (?=1 OR authority_status='canon')
        ORDER BY anchor_id
        """,
        (advantage_id, int(active_only), int(include_noncanon)),
    )


def query_advantage_modules(
    connection: sqlite3.Connection,
    advantage_id: str,
    *,
    enabled_only: bool = False,
    include_noncanon: bool = False,
) -> list[dict[str, Any]]:
    return _query_rows(
        connection,
        """
        SELECT * FROM advantage_module_definitions
        WHERE advantage_id=?
          AND (
            ?=0
            OR module_status IN ('available', 'enabled')
          )
          AND (?=1 OR authority_status='canon')
        ORDER BY stage, module_id
        """,
        (advantage_id, int(enabled_only), int(include_noncanon)),
    )


def query_advantage_runtime(
    connection: sqlite3.Connection,
    advantage_id: str,
    *,
    branch_id: str = "main",
    story_cursor: Mapping[str, Any] | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
) -> dict[str, Any] | None:
    rows = _query_rows(
        connection,
        """
        SELECT * FROM advantage_runtime_state
        WHERE advantage_id=? AND branch_id=?
        """,
        (advantage_id, branch_id),
    )
    if not rows:
        return None
    cursor = _normalize_story_cursor(
        story_cursor,
        chapter_no=chapter_no,
        scene_index=scene_index,
    )
    row = rows[0]
    if cursor is not None and not _coordinate_visible(
        row,
        cursor,
        allow_missing_without_cursor=False,
    ):
        return None
    return row


def query_advantage_ledger(
    connection: sqlite3.Connection,
    advantage_id: str,
    *,
    limit: int = 50,
    entry_kind: str | None = None,
    branch_id: str | None = None,
    story_cursor: Mapping[str, Any] | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
    visible_module_ids: Sequence[str] | None = None,
    visibility: str = "generation",
) -> list[dict[str, Any]]:
    bounded = max(0, min(int(limit), 1000))
    if bounded == 0:
        return []
    normalized_visibility = str(visibility or "generation").strip().casefold()
    if normalized_visibility not in {"generation", "raw", "inspection"}:
        raise ContinuityError(
            "ADVANTAGE_VISIBILITY_MODE_INVALID",
            f"unsupported Advantage visibility mode: {visibility}",
        )
    cursor = _normalize_story_cursor(
        story_cursor,
        chapter_no=chapter_no,
        scene_index=scene_index,
    )
    allowed_modules = (
        {str(value) for value in visible_module_ids if str(value)}
        if visible_module_ids is not None
        else None
    )
    # JSON story coordinates and the visible-module set require post-query
    # filtering.  Do not truncate the candidate rows first or a future/hidden
    # head can starve an older visible ledger entry.
    sql_limit = (
        -1
        if (
            cursor is not None
            or allowed_modules is not None
            or normalized_visibility == "generation"
        )
        else bounded
    )
    rows = _query_rows(
        connection,
        """
        SELECT * FROM advantage_ledger
        WHERE advantage_id=?
          AND (? IS NULL OR entry_kind=?)
          AND (? IS NULL OR branch_id=?)
        ORDER BY updated_order DESC, entry_id DESC
        LIMIT ?
        """,
        (
            advantage_id,
            entry_kind,
            entry_kind,
            branch_id,
            branch_id,
            sql_limit,
        ),
    )
    visible: list[dict[str, Any]] = []
    for row in rows:
        if cursor is not None and not _coordinate_visible(
            row,
            cursor,
            allow_missing_without_cursor=False,
        ):
            continue
        module_id = str(row.get("module_id") or "")
        if (
            allowed_modules is not None
            and module_id
            and module_id not in allowed_modules
        ):
            continue
        if normalized_visibility == "generation":
            input_value = row.get("input_json")
            provenance = row.get("provenance_json")
            if any(
                _contains_author_plan(row.get(field))
                for field in (
                    "input_json",
                    "output_json",
                    "loss_json",
                    "provenance_json",
                )
            ):
                continue
            # Claims live on the knowledge surface.  Even when a claim itself
            # is visible, the reveal ledger exposes author-side scheduling
            # metadata (stage, source and causal position), so generation
            # suppresses reveal entries wholesale.
            if str(row.get("entry_kind") or "") == "reveal":
                continue
            visible.append(
                {
                    key: deepcopy(row.get(key))
                    for key in (
                        "entry_id",
                        "advantage_id",
                        "module_id",
                        "branch_id",
                        "entry_kind",
                        "actor_entity_id",
                        "target_entity_id",
                        "input_json",
                        "output_json",
                        "loss_json",
                    )
                }
            )
            visible[-1]["input_json"] = _generation_safe_payload(
                row.get("input_json")
            )
            visible[-1]["output_json"] = _generation_safe_payload(
                row.get("output_json")
            )
            visible[-1]["loss_json"] = _generation_safe_payload(
                row.get("loss_json")
            )
        else:
            visible.append(row)
        if len(visible) >= bounded:
            break
    return visible


def query_advantage_knowledge(
    connection: sqlite3.Connection,
    advantage_id: str,
    *,
    knowledge_plane: str | None = None,
    observer_entity_id: str | None = None,
    include_noncanon: bool = False,
    visibility: str = "generation",
    reveal_stage: str | None = None,
    visible_reveal_stages: Sequence[str] | None = None,
    story_cursor: Mapping[str, Any] | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
    visible_module_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    if (
        knowledge_plane is not None
        and knowledge_plane not in ADVANTAGE_KNOWLEDGE_PLANES
    ):
        raise ContinuityError(
            "ADVANTAGE_KNOWLEDGE_PLANE_INVALID",
            f"unsupported knowledge plane: {knowledge_plane}",
        )
    normalized_visibility = str(visibility or "generation").strip().casefold()
    if normalized_visibility not in {"generation", "raw", "inspection"}:
        raise ContinuityError(
            "ADVANTAGE_VISIBILITY_MODE_INVALID",
            f"unsupported Advantage visibility mode: {visibility}",
        )
    if (
        normalized_visibility == "generation"
        and knowledge_plane == "actor_belief"
        and not str(observer_entity_id or "").strip()
    ):
        raise ContinuityError(
            "ADVANTAGE_OBSERVER_REQUIRED",
            "actor_belief queries require observer_entity_id",
        )
    rows = _query_rows(
        connection,
        """
        SELECT * FROM advantage_knowledge
        WHERE advantage_id=?
          AND (?=1 OR knowledge_status='canon')
        ORDER BY updated_order DESC, knowledge_id
        """,
        (
            advantage_id,
            int(
                bool(include_noncanon)
                and normalized_visibility != "generation"
            ),
        ),
    )
    cursor = _normalize_story_cursor(
        story_cursor,
        # An explicit chapter/scene passed to the public query API is a real
        # caller-supplied cursor.  Plot hooks that only want the accepted
        # current head deliberately pass neither value, so honoring these
        # parameters here does not recreate the old forged-current-head leak.
        chapter_no=chapter_no,
        scene_index=scene_index,
    )
    allowed_modules = (
        {str(value) for value in visible_module_ids if str(value)}
        if visible_module_ids is not None
        else None
    )
    observer = str(observer_entity_id or "").strip()
    if normalized_visibility == "generation":
        generation_planes = set(ADVANTAGE_GENERATION_KNOWLEDGE_PLANES)
        if observer:
            generation_planes.add("actor_belief")
        allowed_planes = (
            generation_planes.intersection({knowledge_plane})
            if knowledge_plane is not None
            else generation_planes
        )
    else:
        allowed_planes = (
            {knowledge_plane}
            if knowledge_plane is not None
            else set(ADVANTAGE_KNOWLEDGE_PLANES)
        )
    visible: list[dict[str, Any]] = []
    for row in rows:
        plane = str(row.get("knowledge_plane") or "")
        if plane not in allowed_planes:
            continue
        row_observer = str(row.get("observer_entity_id") or "").strip()
        if normalized_visibility == "generation":
            if plane == "actor_belief":
                # Never treat NULL or another actor's belief as a wildcard.
                if not observer or row_observer != observer:
                    continue
            elif row_observer and observer and row_observer != observer:
                continue
            elif row_observer and not observer:
                continue
        elif observer and row_observer and row_observer != observer:
            # Preserve the inspection API's historical "matching observer or
            # global row" behavior.
            continue
        module_id = str(row.get("module_id") or "")
        if (
            allowed_modules is not None
            and module_id
            and module_id not in allowed_modules
        ):
            continue
        if normalized_visibility == "generation":
            if not _reveal_stage_visible(
                row,
                cursor=cursor,
                reveal_stage=reveal_stage,
                visible_reveal_stages=visible_reveal_stages,
            ):
                continue
        elif reveal_stage is not None and str(
            row.get("reveal_stage") or ""
        ) != str(reveal_stage):
            continue
        if normalized_visibility == "generation":
            visible.append(
                {
                    "knowledge_plane": plane,
                    "knowledge_status": str(
                        row.get("knowledge_status") or "canon"
                    ),
                    "observer_entity_id": (
                        row.get("observer_entity_id")
                        if plane == "actor_belief"
                        else None
                    ),
                    "claim": deepcopy(row.get("claim_json") or {}),
                }
            )
        else:
            visible.append(row)
    return visible


def query_advantage_contracts(
    connection: sqlite3.Connection,
    advantage_id: str,
    *,
    active_only: bool = True,
    include_noncanon: bool = False,
    generation_visible_only: bool = False,
    story_cursor: Mapping[str, Any] | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
) -> list[dict[str, Any]]:
    rows = _query_rows(
        connection,
        """
        SELECT * FROM advantage_contracts
        WHERE advantage_id=?
          AND (
            ?=0
            OR contract_status IN (
                'proposed', 'active', 'suspended', 'breached'
            )
          )
          AND (
            ?=0
            OR contract_status IN ('active', 'suspended', 'breached')
          )
          AND (?=1 OR authority_status='canon')
        ORDER BY updated_order DESC, contract_id
        """,
        (
            advantage_id,
            int(active_only),
            int(generation_visible_only),
            int(include_noncanon),
        ),
    )
    cursor = _normalize_story_cursor(
        story_cursor,
        chapter_no=chapter_no,
        scene_index=scene_index,
    )
    if cursor is None:
        return rows
    return [
        row
        for row in rows
        if _coordinate_visible(
            row,
            cursor,
            allow_missing_without_cursor=False,
        )
    ]


def query_advantage_narrative_contract(
    connection: sqlite3.Connection,
    advantage_id: str,
    *,
    active_only: bool = False,
) -> dict[str, Any] | None:
    rows = _query_rows(
        connection,
        """
        SELECT * FROM advantage_narrative_contracts
        WHERE advantage_id=?
          AND authority_status='canon'
          AND (?=0 OR contract_status='active')
        """,
        (advantage_id, int(active_only)),
    )
    return rows[0] if rows else None


def query_advantage_progression(
    connection: sqlite3.Connection,
    advantage_id: str,
    *,
    branch_id: str = "main",
    generation_visible_only: bool = False,
    story_cursor: Mapping[str, Any] | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
    visible_module_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    runtime = query_advantage_runtime(
        connection,
        advantage_id,
        branch_id=branch_id,
        story_cursor=story_cursor,
        chapter_no=chapter_no,
        scene_index=scene_index,
    )
    slots = _query_rows(
        connection,
        """
        SELECT * FROM advantage_runtime_slots
        WHERE advantage_id=?
          AND authority_status='canon'
          AND (
            ?=0
            OR slot_status IN ('available', 'filled')
          )
        ORDER BY stage, slot_id
        """,
        (advantage_id, int(generation_visible_only)),
    )
    allowed_modules = (
        {str(value) for value in visible_module_ids if str(value)}
        if visible_module_ids is not None
        else None
    )
    if allowed_modules is not None:
        slots = [
            row
            for row in slots
            if not str(row.get("module_id") or "")
            or str(row.get("module_id")) in allowed_modules
        ]
    # Runtime is a current-state row rather than a historical snapshot.  When
    # it is newer than the requested cursor, hiding slots is safer than
    # presenting a future unlock graph as if it already existed.
    cursor = _normalize_story_cursor(
        story_cursor,
        chapter_no=chapter_no,
        scene_index=scene_index,
    )
    if generation_visible_only and cursor is not None and runtime is None:
        slots = []
    upgrades = query_advantage_ledger(
        connection,
        advantage_id,
        limit=100,
        entry_kind="upgrade",
        branch_id=branch_id,
        story_cursor=story_cursor,
        chapter_no=(
            chapter_no
            if not generation_visible_only
            else None
        ),
        scene_index=(
            scene_index
            if not generation_visible_only
            else None
        ),
        visible_module_ids=visible_module_ids,
        visibility=(
            "generation" if generation_visible_only else "inspection"
        ),
    )
    return {
        "advantage_id": advantage_id,
        "branch_id": branch_id,
        "stage": runtime.get("stage") if runtime else None,
        "unlocked_modules": (
            runtime.get("unlocked_modules_json") if runtime else []
        ),
        "slots": slots,
        "upgrade_history": upgrades,
    }


def query_advantage_exposure(
    connection: sqlite3.Connection,
    advantage_id: str,
    *,
    branch_id: str = "main",
    generation_visible_only: bool = False,
    story_cursor: Mapping[str, Any] | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
) -> dict[str, Any]:
    runtime = query_advantage_runtime(
        connection,
        advantage_id,
        branch_id=branch_id,
        story_cursor=story_cursor,
        chapter_no=chapter_no,
        scene_index=scene_index,
    )
    return {
        "advantage_id": advantage_id,
        "branch_id": branch_id,
        "pollution": float((runtime or {}).get("pollution") or 0),
        "exposure": float((runtime or {}).get("exposure") or 0),
        "debt": float((runtime or {}).get("debt") or 0),
        "cooldown_until": (
            (runtime or {}).get("cooldown_until_json")
        ),
        "contracts": query_advantage_contracts(
            connection,
            advantage_id,
            active_only=True,
            generation_visible_only=generation_visible_only,
            story_cursor=story_cursor,
            chapter_no=chapter_no,
            scene_index=scene_index,
        ),
    }


def _advantage_module_runtime(
    modules: Sequence[Mapping[str, Any]],
    runtime: Mapping[str, Any] | None,
    slots: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Derive module-level runtime without inventing per-module counters."""

    runtime_map = dict(runtime or {})
    unlocked = {
        str(value)
        for value in runtime_map.get("unlocked_modules_json") or []
        if str(value)
    }
    runtime_enabled = bool(runtime_map.get("enabled"))
    slots_by_module: dict[str, list[dict[str, Any]]] = {}
    for slot in slots:
        slot_module_id = str(slot.get("module_id") or "")
        if not slot_module_id:
            continue
        slots_by_module.setdefault(slot_module_id, []).append(dict(slot))
    rows: list[dict[str, Any]] = []
    for module in modules:
        module_id = str(module.get("module_id") or "")
        if not module_id:
            continue
        module_status = str(module.get("module_status") or "")
        is_unlocked = module_id in unlocked
        rows.append(
            {
                "module_id": module_id,
                "module_status": module_status,
                "stage": str(module.get("stage") or ""),
                "unlocked": is_unlocked,
                "enabled": bool(
                    runtime_enabled
                    and (
                        module_status == "enabled"
                        or is_unlocked
                    )
                ),
                "slots": slots_by_module.get(module_id, []),
            }
        )
    return rows


def query_advantage_context(
    connection: sqlite3.Connection,
    advantage_id: str,
    *,
    branch_id: str = "main",
    knowledge_plane: str | None = None,
    observer_entity_id: str | None = None,
    ledger_limit: int = 10,
    visibility: str = "generation",
    reveal_stage: str | None = None,
    visible_reveal_stages: Sequence[str] | None = None,
    story_cursor: Mapping[str, Any] | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
) -> dict[str, Any]:
    """Build the exact local context used by CLI, MCP and plot hooks."""

    normalized_visibility = str(visibility or "generation").strip().casefold()
    generation = normalized_visibility == "generation"
    definition = query_advantage_definition(connection, advantage_id)
    if generation and (
        not isinstance(definition, Mapping)
        or str(definition.get("advantage_status") or "") != "canon"
        or str(definition.get("lifecycle_status") or "")
        not in ADVANTAGE_VISIBLE_LIFECYCLE_STATUSES
    ):
        definition = None
    if definition is None:
        return {
            "schema_version": ADVANTAGE_SCHEMA_VERSION,
            "visibility": normalized_visibility,
            "visibility_status": "not_visible",
            "definition": None,
            "anchors": [],
            "modules": [],
            "module_runtime": [],
            "runtime": None,
            "ledger": [],
            "knowledge": [],
            "contracts": [],
            "narrative_contract": None,
            "progression": {
                "advantage_id": advantage_id,
                "branch_id": branch_id,
                "stage": None,
                "unlocked_modules": [],
                "slots": [],
                "upgrade_history": [],
            },
            "exposure": {
                "advantage_id": advantage_id,
                "branch_id": branch_id,
                "pollution": 0.0,
                "exposure": 0.0,
                "debt": 0.0,
                "cooldown_until": None,
                "contracts": [],
            },
        }

    cursor = _normalize_story_cursor(
        story_cursor,
        chapter_no=chapter_no,
        scene_index=scene_index,
    )
    anchors = query_advantage_anchors(connection, advantage_id)
    if generation and cursor is not None:
        anchors = [
            row
            for row in anchors
            if _coordinate_visible(
                row,
                cursor,
                allow_missing_without_cursor=False,
            )
        ]
    modules = query_advantage_modules(
        connection,
        advantage_id,
        enabled_only=generation,
        include_noncanon=False,
    )
    runtime = query_advantage_runtime(
        connection,
        advantage_id,
        branch_id=branch_id,
        story_cursor=(cursor if generation else None),
    )
    # The runtime row is the latest branch state.  If it lies after the
    # requested cursor, no historical module snapshot exists in v1, so hide
    # all runtime-dependent module surfaces rather than leak a future unlock.
    projection_ahead_of_cursor = bool(
        generation
        and cursor is not None
        and runtime is None
        and query_advantage_runtime(
            connection,
            advantage_id,
            branch_id=branch_id,
        )
        is not None
    )
    if projection_ahead_of_cursor:
        modules = []
    visible_module_ids = [
        str(row.get("module_id") or "")
        for row in modules
        if str(row.get("module_id") or "")
    ]
    knowledge = query_advantage_knowledge(
        connection,
        advantage_id,
        knowledge_plane=knowledge_plane,
        observer_entity_id=observer_entity_id,
        include_noncanon=False,
        visibility=normalized_visibility,
        reveal_stage=reveal_stage,
        visible_reveal_stages=visible_reveal_stages,
        story_cursor=(cursor if generation else None),
        visible_module_ids=(
            visible_module_ids if generation else None
        ),
    )
    ledger = query_advantage_ledger(
        connection,
        advantage_id,
        limit=ledger_limit,
        branch_id=branch_id,
        story_cursor=(cursor if generation else None),
        visibility=normalized_visibility,
        visible_module_ids=(
            visible_module_ids if generation else None
        ),
    )
    contracts = query_advantage_contracts(
        connection,
        advantage_id,
        generation_visible_only=generation,
        story_cursor=(cursor if generation else None),
    )
    progression = query_advantage_progression(
        connection,
        advantage_id,
        branch_id=branch_id,
        generation_visible_only=generation,
        story_cursor=(cursor if generation else None),
        visible_module_ids=(
            visible_module_ids if generation else None
        ),
    )
    exposure = query_advantage_exposure(
        connection,
        advantage_id,
        branch_id=branch_id,
        generation_visible_only=generation,
        story_cursor=(cursor if generation else None),
    )
    narrative_contract = query_advantage_narrative_contract(
        connection,
        advantage_id,
        active_only=generation,
    )
    if generation and isinstance(narrative_contract, Mapping):
        narrative_contract = {
            key: deepcopy(value)
            for key, value in narrative_contract.items()
            if key != "reveal_ladder_json"
        }
    return {
        "schema_version": ADVANTAGE_SCHEMA_VERSION,
        "visibility": normalized_visibility,
        "visibility_status": (
            "cursor_ahead_unknown"
            if projection_ahead_of_cursor
            else "visible"
        ),
        "story_cursor": dict(cursor or {}),
        "definition": definition,
        "anchors": anchors,
        "modules": modules,
        "module_runtime": _advantage_module_runtime(
            modules,
            runtime,
            progression.get("slots") or [],
        ),
        "runtime": runtime,
        "ledger": ledger,
        "knowledge": knowledge,
        "contracts": contracts,
        "narrative_contract": narrative_contract,
        "progression": progression,
        "exposure": exposure,
    }


def query_advantage_contexts(
    connection: sqlite3.Connection,
    advantage_ids: Sequence[str] | None = None,
    *,
    owner_entity_id: str | None = None,
    branch_id: str = "main",
    knowledge_plane: str | None = None,
    observer_entity_id: str | None = None,
    ledger_limit: int = 10,
    visibility: str = "generation",
    reveal_stage: str | None = None,
    visible_reveal_stages: Sequence[str] | None = None,
    story_cursor: Mapping[str, Any] | None = None,
    chapter_no: int | None = None,
    scene_index: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Build context cards for explicit IDs or every matching definition."""

    generation = str(visibility or "generation").strip().casefold() == "generation"
    ids = (
        [str(value) for value in advantage_ids]
        if advantage_ids is not None
        else [
            str(row["advantage_id"])
            for row in query_advantage_definitions(
                connection,
                owner_entity_id=owner_entity_id,
                branch_id=branch_id,
                generation_visible_only=generation,
            )
        ]
    )
    normalized_ids = sorted(set(ids))
    if limit is not None:
        normalized_ids = normalized_ids[: max(0, int(limit))]
    contexts = [
        query_advantage_context(
            connection,
            advantage_id,
            branch_id=branch_id,
            knowledge_plane=knowledge_plane,
            observer_entity_id=observer_entity_id,
            ledger_limit=ledger_limit,
            visibility=visibility,
            reveal_stage=reveal_stage,
            visible_reveal_stages=visible_reveal_stages,
            story_cursor=story_cursor,
            chapter_no=chapter_no,
            scene_index=scene_index,
        )
        for advantage_id in normalized_ids
    ]
    # Explicit IDs do not bypass canon/lifecycle visibility and unknown IDs do
    # not produce zero-value cards that the renderer could mistake for facts.
    return [
        context
        for context in contexts
        if isinstance(context.get("definition"), Mapping)
    ]


__all__ = [
    "ADVANTAGE_ANCHOR_TYPES",
    "ADVANTAGE_BRANCH_LOCAL_EVENT_TYPES",
    "ADVANTAGE_EVENT_TYPES",
    "ADVANTAGE_KNOWLEDGE_PLANES",
    "ADVANTAGE_LEDGER_KINDS",
    "ADVANTAGE_META_ACTIVE_REVISION",
    "ADVANTAGE_META_HASH",
    "ADVANTAGE_META_HEAD_REVISION",
    "ADVANTAGE_META_VERSION",
    "ADVANTAGE_PROJECTION_SCHEMA_VERSION",
    "ADVANTAGE_PROJECTION_INDEXES",
    "ADVANTAGE_PROJECTION_TABLES",
    "ADVANTAGE_SCHEMA_SQL",
    "ADVANTAGE_SCHEMA_VERSION",
    "ADVANTAGE_STATUSES",
    "AdvantageProjectionState",
    "advantage_schema_ready",
    "advantage_projection_payload",
    "bootstrap_advantage_projection",
    "canonical_json",
    "compute_advantage_projection_hash",
    "ensure_advantage_schema",
    "load_advantage_sidecar",
    "query_advantage_anchors",
    "query_advantage_context",
    "query_advantage_contexts",
    "query_advantage_contracts",
    "query_advantage_definition",
    "query_advantage_definitions",
    "query_advantage_exposure",
    "query_advantage_knowledge",
    "query_advantage_ledger",
    "query_advantage_modules",
    "query_advantage_narrative_contract",
    "query_advantage_progression",
    "query_advantage_runtime",
    "read_advantage_projection_metadata",
    "rebuild_advantage_projection",
    "refresh_advantage_projection_metadata",
    "stable_advantage_id",
    "validate_advantage_event_sequence",
]
