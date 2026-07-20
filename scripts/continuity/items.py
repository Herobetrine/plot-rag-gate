"""Schema-v6 item projection helpers.

The item projection is intentionally independent from the established
continuity projection hash.  Schema migration may register legacy item
entities and explicit unique inventory identities, but it never manufactures
functions, stacks, custody semantics or accepted bootstrap events.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .schema import ITEM_PROJECTION_SCHEMA_VERSION, ITEM_PROJECTION_TABLES
from .validators import ContinuityError

ITEM_PROJECTION_META_VERSION = "schema_version"
ITEM_PROJECTION_META_HASH = "projection_hash"
ITEM_PROJECTION_META_HEAD_REVISION = "source_head_revision"
ITEM_PROJECTION_META_ACTIVE_REVISION = "source_active_revision"

LEGACY_ITEM_UNMODELED = "legacy_unmodeled"
LEGACY_ITEM_SELF_INSTANCE = "legacy_self_instance"

ITEM_DELTA_SCHEMA_VERSION = "plot-rag-delta/v4"
ITEM_EVENT_TYPES = frozenset(
    {
        "item_spec",
        "item_instance",
        "item_custody",
        "item_runtime",
        "item_function_runtime",
        "item_use",
        "item_observation",
        "item_correction",
    }
)
ITEM_FUNCTION_RUNTIME_ACTIONS = frozenset(
    {
        "bootstrap",
        "enable",
        "disable",
        "unlock",
        "lock",
        "suppress",
        "set_charges",
        "set_cooldown",
        "clear_cooldown",
    }
)
ITEM_FUNCTION_UNLOCK_STATES = frozenset(
    {"locked", "unlocked", "suppressed"}
)
ITEM_BINDING_STATUSES = frozenset(
    {"active", "deprecated", "superseded"}
)
ITEM_ACTIVATION_KINDS = frozenset(
    {"active", "passive", "toggle", "reaction", "triggered"}
)
ITEM_STACK_POLICIES = frozenset(
    {"non_stackable", "homogeneous", "lot", "unknown"}
)
ITEM_UNIQUENESS_POLICIES = frozenset(
    {"ordinary", "unique_instance", "unique_definition", "unknown"}
)
ITEM_CUSTODY_STATUSES = frozenset(
    {
        "possessed",
        "stored",
        "loaned",
        "seized",
        "lost",
        "abandoned",
        "in_transit",
        "destroyed",
        "unknown",
    }
)
ITEM_KNOWLEDGE_PLANES = frozenset(
    {
        "objective",
        "actor_belief",
        "public_narrative",
        "reader_disclosed",
        "author_plan",
    }
)
ITEM_TARGET_KINDS = frozenset(
    {
        "none",
        "self",
        "actor",
        "any",
        "entity",
        "character",
        "item",
        "location",
        "organization",
        "faction",
    }
)
ITEM_RANGE_KINDS = frozenset(
    {
        "self",
        "touch",
        "same_location",
        "line_of_sight",
        "distance",
        "remote",
        "unbounded",
    }
)
_OPTIONAL_ITEM_PROJECTION_TABLES = frozenset(
    {
        "item_stack_function_runtime_state",
        "item_knowledge_observations",
    }
)
_ITEM_TABLE_DELETE_ORDER = (
    "item_projection_meta",
    "item_knowledge_observations",
    "item_observations",
    "item_use_history",
    "item_stack_function_runtime_state",
    "item_function_runtime_state",
    "item_runtime_state",
    "item_custody_state",
    "item_function_bindings",
    "item_function_definitions",
    "item_stacks",
    "item_instances",
    "item_definitions",
)
_JSON_COLUMNS = frozenset(
    {
        "definition_json",
        "instance_json",
        "batch_json",
        "binding_json",
        "state_json",
        "story_coordinate_json",
        "cooldown_until_json",
        "delta_json",
        "before_json",
        "after_json",
        "observation_json",
    }
)
_EPSILON = 1e-9


@dataclass(frozen=True)
class ItemRolloutPolicy:
    """Project-level rollout switches for schema-v6 item authority.

    ``strict_runtime_validation=False`` is deliberately shadow-only.  The
    reducer may still dry-run proposed v4 events and report deterministic
    diagnostics, but an authority-changing accept must not publish those
    events until the project explicitly enables strict validation.
    """

    strict_runtime_validation: bool = False
    power_binding_bridge: bool = True

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
    ) -> "ItemRolloutPolicy":
        raw = dict(value or {})
        strict = raw.get("strict_runtime_validation", False)
        bridge = raw.get("power_binding_bridge", True)
        if not isinstance(strict, bool):
            raise ContinuityError(
                "ITEM_ROLLOUT_CONFIG_INVALID",
                "items.strict_runtime_validation must be a boolean",
                details={"field": "items.strict_runtime_validation"},
            )
        if not isinstance(bridge, bool):
            raise ContinuityError(
                "ITEM_ROLLOUT_CONFIG_INVALID",
                "items.power_binding_bridge must be a boolean",
                details={"field": "items.power_binding_bridge"},
            )
        return cls(
            strict_runtime_validation=strict,
            power_binding_bridge=bridge,
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "strict_runtime_validation": self.strict_runtime_validation,
            "power_binding_bridge": self.power_binding_bridge,
        }


STRICT_ITEM_ROLLOUT_POLICY = ItemRolloutPolicy(
    strict_runtime_validation=True,
    power_binding_bridge=True,
)


def load_item_rollout_policy(project_root: str | Path) -> ItemRolloutPolicy:
    """Read only the item rollout block without importing the CLI config layer."""

    root = Path(project_root).expanduser().resolve()
    config_path = root / ".plot-rag" / "config.json"
    if not config_path.is_file():
        return ItemRolloutPolicy()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContinuityError(
            "ITEM_ROLLOUT_CONFIG_INVALID",
            "project item rollout config cannot be read",
            details={"path": str(config_path)},
        ) from exc
    if not isinstance(payload, Mapping):
        raise ContinuityError(
            "ITEM_ROLLOUT_CONFIG_INVALID",
            "project config root must be an object",
            details={"path": str(config_path)},
        )
    items = payload.get("items")
    if items is not None and not isinstance(items, Mapping):
        raise ContinuityError(
            "ITEM_ROLLOUT_CONFIG_INVALID",
            "config.items must be an object",
            details={"path": str(config_path)},
        )
    return ItemRolloutPolicy.from_mapping(
        items if isinstance(items, Mapping) else None
    )


def canonical_json(value: object) -> str:
    """Return the deterministic JSON representation used for item hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_item_id(prefix: str, value: object) -> str:
    """Create a deterministic item identifier without relying on timestamps."""

    raw = canonical_json(value).encode("utf-8")
    return prefix + hashlib.sha256(raw).hexdigest()


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})")
    ]


def item_projection_payload(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    """Read the stable, item-only projection payload.

    This mirrors the established replay hash discipline while remaining
    separate from ``PROJECTION_TABLES``.  Control-plane tables and
    ``item_projection_meta`` are deliberately excluded.
    """

    payload: dict[str, Any] = {
        "schema_version": ITEM_PROJECTION_SCHEMA_VERSION,
        "tables": {},
    }
    existing_tables = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table'
            """
        )
    }
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in ITEM_PROJECTION_TABLES:
        # A v6 database is validated before the v7 additive migration creates
        # the two new item runtime/knowledge tables.  Omitting absent tables
        # preserves the historical v6 projection hash; once migrated, the
        # tables are present and become part of the v7 hash automatically.
        if table not in existing_tables:
            continue
        columns = _table_columns(connection, table)
        if not columns and table in _OPTIONAL_ITEM_PROJECTION_TABLES:
            continue
        stable_columns = [
            column
            for column in columns
            if column not in {"created_at", "updated_at", "completed_at"}
        ]
        if not stable_columns:
            tables[table] = []
            continue
        selected = ", ".join(stable_columns)
        rows = connection.execute(
            f"SELECT {selected} FROM {table} ORDER BY {selected}"
        ).fetchall()
        if not rows and table in _OPTIONAL_ITEM_PROJECTION_TABLES:
            continue
        tables[table] = [
            {
                column: row[column]
                if isinstance(row, (sqlite3.Row, Mapping))
                else row[index]
                for index, column in enumerate(stable_columns)
            }
            for row in rows
        ]
    payload["tables"] = tables
    return payload


def compute_item_projection_hash(connection: sqlite3.Connection) -> str:
    """Hash only the schema-v6 item projection."""

    raw = canonical_json(item_projection_payload(connection)).encode("utf-8")
    return "item_projection_" + hashlib.sha256(raw).hexdigest()


def _meta_int(
    connection: sqlite3.Connection,
    key: str,
    default: int = 0,
) -> int:
    row = connection.execute(
        "SELECT value FROM state_meta WHERE key=?",
        (key,),
    ).fetchone()
    return int(row[0]) if row is not None else int(default)


def refresh_item_projection_metadata(
    connection: sqlite3.Connection,
    *,
    source_event_id: str | None = None,
    updated_order: int | None = None,
) -> str:
    """Persist the independent item projection version and hash."""

    if updated_order is None:
        updated_order = 0
        for table in ITEM_PROJECTION_TABLES:
            columns = _table_columns(connection, table)
            if "updated_order" not in columns:
                continue
            row = connection.execute(
                f"SELECT MAX(updated_order) FROM {table}"
            ).fetchone()
            updated_order = max(updated_order, int(row[0] or 0))

    projection_hash = compute_item_projection_hash(connection)
    metadata = {
        ITEM_PROJECTION_META_VERSION: ITEM_PROJECTION_SCHEMA_VERSION,
        ITEM_PROJECTION_META_HASH: projection_hash,
        ITEM_PROJECTION_META_HEAD_REVISION: _meta_int(
            connection, "head_canon_revision"
        ),
        ITEM_PROJECTION_META_ACTIVE_REVISION: _meta_int(
            connection, "active_canon_revision"
        ),
    }
    for key, value in metadata.items():
        connection.execute(
            """
            INSERT INTO item_projection_meta(
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


def read_item_projection_metadata(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    """Decode the public item projection metadata surface."""

    result: dict[str, Any] = {}
    for row in connection.execute(
        """
        SELECT meta_key, value_json
        FROM item_projection_meta
        ORDER BY meta_key
        """
    ):
        result[str(row[0])] = json.loads(str(row[1]))
    return result


def _legacy_attributes(raw: object) -> Any:
    text = str(raw or "{}")
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"legacy_raw": text}


def seed_legacy_item_projection(connection: sqlite3.Connection) -> None:
    """Derive replayable legacy item identities without inventing semantics.

    This helper is intentionally version-independent so an item replay can
    call it after rebuilding the legacy ``inventory_state`` projection.

    * every existing ``entity_type=item`` becomes a legacy definition;
    * an explicit ``inventory_state.is_unique=1`` flag permits one derived
      ``legacy_self_instance`` identity;
    * ordinary inventory remains ``legacy_unmodeled``;
    * no stack, function, binding, custody, runtime, use, observation,
      continuity event or canon commit is created.
    """

    original_row_factory = connection.row_factory
    if original_row_factory is None:
        connection.row_factory = sqlite3.Row
    try:
        entities = connection.execute(
            """
            SELECT entity_id, canonical_name, attributes_json
            FROM entities
            WHERE entity_type='item'
            ORDER BY entity_id
            """
        ).fetchall()
        for entity in entities:
            entity_id = str(entity["entity_id"])
            inventory_rows = connection.execute(
                """
                SELECT inventory_key, owner_entity_id, quantity, is_unique,
                       item_status, source_event_id, updated_order
                FROM inventory_state
                WHERE item_entity_id=?
                ORDER BY updated_order DESC, inventory_key ASC
                """,
                (entity_id,),
            ).fetchall()
            unique_rows = [
                row for row in inventory_rows if int(row["is_unique"]) == 1
            ]
            modeling_status = (
                LEGACY_ITEM_SELF_INSTANCE
                if unique_rows
                else LEGACY_ITEM_UNMODELED
            )
            source_row = inventory_rows[0] if inventory_rows else None
            raw_source_event_id = (
                source_row["source_event_id"]
                if source_row is not None
                else None
            )
            source_event_id = (
                str(raw_source_event_id)
                if raw_source_event_id is not None
                else None
            )
            updated_order = (
                int(source_row["updated_order"])
                if source_row is not None
                else 0
            )
            definition_id = stable_item_id(
                "item_definition_legacy_",
                {"item_entity_id": entity_id},
            )
            legacy_attributes = _legacy_attributes(entity["attributes_json"])
            definition = {
                "schema_version": "plot-rag-item/v1",
                "modeling_status": modeling_status,
                "canonical_name": str(entity["canonical_name"]),
                "legacy": {
                    "item_entity_id": entity_id,
                    "attributes": legacy_attributes,
                    "inventory_keys": [
                        str(row["inventory_key"]) for row in inventory_rows
                    ],
                },
                "unmodeled_fields": [
                    "functions",
                    "stack_identity",
                    "custody_semantics",
                    "runtime",
                ],
            }
            connection.execute(
                """
                INSERT INTO item_definitions(
                    item_definition_id, item_entity_id, item_status,
                    item_kind, stack_policy, uniqueness_policy,
                    definition_json, source_event_id, updated_order
                ) VALUES(?, ?, ?, 'unknown', 'unknown', ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    definition_id,
                    entity_id,
                    modeling_status,
                    "unique_instance" if unique_rows else "unknown",
                    canonical_json(definition),
                    source_event_id,
                    updated_order,
                ),
            )
            if not unique_rows:
                continue

            instance_id = stable_item_id(
                "item_instance_legacy_self_",
                {"item_entity_id": entity_id},
            )
            instance = {
                "schema_version": "plot-rag-item/v1",
                "modeling_status": LEGACY_ITEM_SELF_INSTANCE,
                "instance_name": str(entity["canonical_name"]),
                "unique": True,
                "legacy": {
                    "item_entity_id": entity_id,
                    "attributes": legacy_attributes,
                    "inventory_keys": [
                        str(row["inventory_key"]) for row in unique_rows
                    ],
                },
                "unmodeled_fields": [
                    "functions",
                    "custody_semantics",
                    "runtime",
                ],
            }
            unique_source = unique_rows[0]["source_event_id"]
            connection.execute(
                """
                INSERT INTO item_instances(
                    item_instance_id, item_definition_id, item_entity_id,
                    instance_status, instance_json, source_event_id,
                    story_coordinate_json, updated_order
                ) VALUES(?, ?, ?, ?, ?, ?, '{}', ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    instance_id,
                    definition_id,
                    entity_id,
                    LEGACY_ITEM_SELF_INSTANCE,
                    canonical_json(instance),
                    (
                        str(unique_source)
                        if unique_source is not None
                        else None
                    ),
                    int(unique_rows[0]["updated_order"]),
                ),
            )
    finally:
        connection.row_factory = original_row_factory


def migrate_legacy_item_projection(
    connection: sqlite3.Connection,
    *,
    from_version: int,
) -> str:
    """Seed the additive v6 projection and its independent metadata."""

    # ``from_version`` remains explicit in the migration API so callers cannot
    # accidentally confuse this projection-only step with accepted-event
    # import.  Derivation itself is version-independent and replayable.
    int(from_version)
    seed_legacy_item_projection(connection)
    return refresh_item_projection_metadata(connection)


def _decode_json(value: Any, fallback: Any) -> Any:
    if value is None or value == "":
        return deepcopy(fallback)
    if isinstance(value, (dict, list, int, float, bool)):
        return deepcopy(value)
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return deepcopy(fallback)


def _row_mapping(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for column in _JSON_COLUMNS.intersection(result):
        fallback: Any = None if column == "cooldown_until_json" else {}
        result[column] = _decode_json(result[column], fallback)
    return result


def _finite_number(
    value: Any,
    *,
    field: str,
    minimum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise ContinuityError(
            "ITEM_INVALID_NUMBER",
            f"{field} must be numeric",
            details={"field": field, "value": value},
        )
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContinuityError(
            "ITEM_INVALID_NUMBER",
            f"{field} must be numeric",
            details={"field": field, "value": value},
        ) from exc
    if not math.isfinite(number):
        raise ContinuityError(
            "ITEM_INVALID_NUMBER",
            f"{field} must be finite",
            details={"field": field, "value": value},
        )
    if minimum is not None and number < minimum - _EPSILON:
        raise ContinuityError(
            "ITEM_CONSERVATION_VIOLATION",
            f"{field} cannot be below {minimum}",
            details={"field": field, "value": number, "minimum": minimum},
        )
    if abs(number) <= _EPSILON:
        return 0.0
    return number


def _same_number(left: Any, right: Any) -> bool:
    return math.isclose(
        float(left or 0.0),
        float(right or 0.0),
        rel_tol=0.0,
        abs_tol=_EPSILON,
    )


def _subject(event: Mapping[str, Any]) -> tuple[str, str]:
    subject_type = str(event.get("subject_type") or "").strip()
    subject_id = str(event.get("subject_id") or "").strip()
    instance_id = str(event.get("item_instance_id") or "").strip()
    stack_id = str(event.get("stack_id") or "").strip()
    if instance_id and stack_id:
        raise ContinuityError(
            "ITEM_SUBJECT_AMBIGUOUS",
            "item event cannot address an instance and stack together",
        )
    if not subject_type:
        subject_type = (
            "item_instance"
            if instance_id
            else "item_stack"
            if stack_id
            else ""
        )
    if subject_type == "item_instance" and stack_id:
        raise ContinuityError(
            "ITEM_SUBJECT_AMBIGUOUS",
            "item_instance subject cannot also contain stack_id",
        )
    if subject_type == "item_stack" and instance_id:
        raise ContinuityError(
            "ITEM_SUBJECT_AMBIGUOUS",
            "item_stack subject cannot also contain item_instance_id",
        )
    if not subject_id:
        subject_id = instance_id or stack_id
    if subject_type not in {"item_instance", "item_stack"} or not subject_id:
        raise ContinuityError(
            "ITEM_SUBJECT_REQUIRED",
            "item event requires one item instance or stack",
        )
    typed_id = instance_id if subject_type == "item_instance" else stack_id
    if typed_id and typed_id != subject_id:
        raise ContinuityError(
            "ITEM_SUBJECT_MISMATCH",
            "subject_id conflicts with the typed item identifier",
            details={
                "subject_type": subject_type,
                "subject_id": subject_id,
                "typed_id": typed_id,
            },
        )
    return subject_type, subject_id


def _observation_subject(event: Mapping[str, Any]) -> tuple[str, str]:
    definition_id = str(event.get("item_definition_id") or "").strip()
    instance_id = str(event.get("item_instance_id") or "").strip()
    stack_id = str(event.get("stack_id") or "").strip()
    subject_type = str(event.get("subject_type") or "").strip()
    subject_id = str(event.get("subject_id") or "").strip()
    populated = [
        value
        for value in (definition_id, instance_id, stack_id)
        if value
    ]
    if len(populated) > 1:
        raise ContinuityError(
            "ITEM_SUBJECT_AMBIGUOUS",
            "item observation must address exactly one definition, instance, or stack",
        )
    if not subject_type:
        subject_type = (
            "item_definition"
            if definition_id
            else "item_instance"
            if instance_id
            else "item_stack"
            if stack_id
            else ""
        )
    if not subject_id:
        subject_id = definition_id or instance_id or stack_id
    if subject_type not in {
        "item_definition",
        "item_instance",
        "item_stack",
    } or not subject_id:
        raise ContinuityError(
            "ITEM_SUBJECT_REQUIRED",
            "item observation requires a definition, instance, or stack subject",
        )
    typed_id = {
        "item_definition": definition_id,
        "item_instance": instance_id,
        "item_stack": stack_id,
    }[subject_type]
    if typed_id and typed_id != subject_id:
        raise ContinuityError(
            "ITEM_SUBJECT_MISMATCH",
            "subject_id conflicts with the typed item identifier",
            details={
                "subject_type": subject_type,
                "subject_id": subject_id,
                "typed_id": typed_id,
            },
        )
    if populated and populated[0] != subject_id:
        raise ContinuityError(
            "ITEM_SUBJECT_MISMATCH",
            "subject_type conflicts with the typed item identifier",
            details={
                "subject_type": subject_type,
                "subject_id": subject_id,
                "typed_id": populated[0],
            },
        )
    return subject_type, subject_id


def _function_runtime_key(
    subject_type: str,
    subject_id: str,
    function_id: str,
) -> tuple[str, str, str]:
    return (str(subject_type), str(subject_id), str(function_id))


def _require_boolean(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ContinuityError(
            "ITEM_INVALID_BOOLEAN",
            f"{field} must be a boolean",
            details={"field": field, "value": value},
        )
    return value


def _require_enum(
    value: Any,
    *,
    field: str,
    choices: Iterable[str],
) -> str:
    text = str(value or "").strip()
    allowed = frozenset(str(choice) for choice in choices)
    if text not in allowed:
        raise ContinuityError(
            "ITEM_INVALID_ENUM",
            f"{field} is not supported",
            details={
                "field": field,
                "value": value,
                "choices": sorted(allowed),
            },
        )
    return text


def _coordinate(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    calendar_id = str(value.get("calendar_id") or "").strip()
    ordinal = value.get("ordinal")
    if not calendar_id or type(ordinal) is not int:
        return None
    result = {"calendar_id": calendar_id, "ordinal": int(ordinal)}
    for field in ("label", "precision", "source_event_id"):
        if value.get(field) is not None:
            result[field] = str(value[field])
    return result


def _cooldown_ready(
    current: Mapping[str, Any] | None,
    cooldown_until: Mapping[str, Any] | None,
) -> bool:
    if cooldown_until is None:
        return True
    if current is None:
        raise ContinuityError(
            "ITEM_STORY_COORDINATE_REQUIRED",
            "item cooldown cannot be evaluated without story_coordinate",
        )
    if str(current.get("calendar_id")) != str(
        cooldown_until.get("calendar_id")
    ):
        raise ContinuityError(
            "ITEM_STORY_COORDINATE_CONFLICT",
            "item cooldown and action use different story calendars",
            details={
                "current": dict(current),
                "cooldown_until": dict(cooldown_until),
            },
        )
    return int(current["ordinal"]) >= int(cooldown_until["ordinal"])


def _add_cooldown(
    coordinate: Mapping[str, Any] | None,
    delta: int | float,
) -> dict[str, Any]:
    if coordinate is None:
        raise ContinuityError(
            "ITEM_STORY_COORDINATE_REQUIRED",
            "item cooldown requires story_coordinate",
        )
    whole = int(delta)
    if whole != float(delta) or whole < 0:
        raise ContinuityError(
            "ITEM_INVALID_COOLDOWN",
            "item cooldown must be a non-negative whole coordinate delta",
            details={"cooldown": delta},
        )
    return {
        "calendar_id": str(coordinate["calendar_id"]),
        "ordinal": int(coordinate["ordinal"]) + whole,
    }


def _event_payload(
    row: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    mapped = dict(row)
    event_type = str(mapped.get("event_type") or "")
    payload = dict(_decode_json(mapped.get("payload_json"), {}))
    if event_type == "item_correction":
        if str(payload.get("action") or "") == "retract":
            return event_type, {}
        replacement = payload.get("replacement")
        if not isinstance(replacement, Mapping):
            return event_type, {}
        payload = dict(replacement)
        event_type = str(payload.get("event_type") or "")
    return event_type, payload


class ItemProjectionState:
    """Pure deterministic reducer state for schema-v6 item projections.

    The same reducer is used twice: acceptance validates a proposed event
    sequence against the currently accepted projection without writing, while
    replay starts from legacy-derived rows and rebuilds every item table from
    immutable accepted events.  This prevents model-supplied before/after
    values from becoming authority and keeps validation/replay semantics equal.
    """

    def __init__(
        self,
        *,
        rollout_policy: ItemRolloutPolicy | None = None,
    ) -> None:
        self.rollout_policy = rollout_policy or STRICT_ITEM_ROLLOUT_POLICY
        self.definitions: dict[str, dict[str, Any]] = {}
        self.instances: dict[str, dict[str, Any]] = {}
        self.stacks: dict[str, dict[str, Any]] = {}
        self.functions: dict[str, dict[str, Any]] = {}
        self.bindings: dict[str, dict[str, Any]] = {}
        self.custody: dict[tuple[str, str], dict[str, Any]] = {}
        self.runtime: dict[str, dict[str, Any]] = {}
        self.function_runtime: dict[
            tuple[str, str, str], dict[str, Any]
        ] = {}
        self.use_history: dict[str, dict[str, Any]] = {}
        self.observations: dict[str, dict[str, Any]] = {}
        self.locations: dict[str, str | None] = {}
        self.entity_types: dict[str, str] = {}
        self.active_ability_definitions: set[str] = set()
        self.power_bindings: list[dict[str, Any]] = []
        self.active_qualifications: set[tuple[str, str]] = set()

    @classmethod
    def from_connection(
        cls,
        connection: sqlite3.Connection,
        *,
        rollout_policy: ItemRolloutPolicy | None = None,
    ) -> "ItemProjectionState":
        state = cls(rollout_policy=rollout_policy)
        table_targets = (
            ("item_definitions", state.definitions, "item_definition_id"),
            ("item_instances", state.instances, "item_instance_id"),
            ("item_stacks", state.stacks, "stack_id"),
            ("item_function_definitions", state.functions, "function_id"),
            ("item_function_bindings", state.bindings, "binding_id"),
            ("item_runtime_state", state.runtime, "item_instance_id"),
            ("item_use_history", state.use_history, "source_event_id"),
        )
        for table, target, key_column in table_targets:
            for raw_row in connection.execute(
                f"SELECT * FROM {table} ORDER BY {key_column}"
            ):
                row = _row_mapping(raw_row)
                target[str(row[key_column])] = row
        for raw_row in connection.execute(
            """
            SELECT * FROM item_custody_state
            ORDER BY subject_type, subject_id
            """
        ):
            row = _row_mapping(raw_row)
            state.custody[
                (str(row["subject_type"]), str(row["subject_id"]))
            ] = row
        for raw_row in connection.execute(
            """
            SELECT * FROM item_function_runtime_state
            ORDER BY item_instance_id, function_id
            """
        ):
            row = _row_mapping(raw_row)
            state.function_runtime[
                _function_runtime_key(
                    "item_instance",
                    str(row["item_instance_id"]),
                    str(row["function_id"]),
                )
            ] = row
        if _table_columns(connection, "item_stack_function_runtime_state"):
            for raw_row in connection.execute(
                """
                SELECT * FROM item_stack_function_runtime_state
                ORDER BY stack_id, function_id
                """
            ):
                row = _row_mapping(raw_row)
                state.function_runtime[
                    _function_runtime_key(
                        "item_stack",
                        str(row["stack_id"]),
                        str(row["function_id"]),
                    )
                ] = row
        for raw_row in connection.execute(
            """
            SELECT * FROM item_observations
            ORDER BY observation_key
            """
        ):
            row = _row_mapping(raw_row)
            subject_type = (
                "item_instance"
                if row.get("item_instance_id") is not None
                else "item_stack"
            )
            subject_id = str(
                row.get("item_instance_id") or row.get("stack_id") or ""
            )
            row.setdefault("subject_type", subject_type)
            row.setdefault("subject_id", subject_id)
            row.setdefault("item_definition_id", None)
            state.observations[str(row["observation_key"])] = row
        if _table_columns(connection, "item_knowledge_observations"):
            for raw_row in connection.execute(
                """
                SELECT * FROM item_knowledge_observations
                ORDER BY observation_key
                """
            ):
                row = _row_mapping(raw_row)
                state.observations[str(row["observation_key"])] = row
        state.locations = {
            str(row["actor_entity_id"]): (
                str(row["location_entity_id"])
                if row["location_entity_id"] is not None
                else None
            )
            for row in connection.execute(
                "SELECT actor_entity_id, location_entity_id FROM location_state"
            )
        }
        state.entity_types = {
            str(row["entity_id"]): str(row["entity_type"])
            for row in connection.execute(
                "SELECT entity_id, entity_type FROM entities"
            )
        }
        state.active_ability_definitions = {
            str(row["ability_entity_id"])
            for row in connection.execute(
                """
                SELECT ability_entity_id
                FROM ability_definitions
                WHERE definition_status!='deprecated'
                """
            )
        }
        state.power_bindings = [
            _row_mapping(row)
            for row in connection.execute(
                """
                SELECT * FROM power_bindings
                WHERE active=1
                ORDER BY actor_entity_id, binding_id
                """
            )
        ]
        state.active_qualifications = {
            (
                str(row["actor_entity_id"]),
                str(row["qualification_entity_id"]),
            )
            for row in connection.execute(
                """
                SELECT actor_entity_id, qualification_entity_id
                FROM qualification_state
                WHERE active=1 AND quantity > 0
                """
            )
        }
        return state

    @staticmethod
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

    @staticmethod
    def _insert_row(
        connection: sqlite3.Connection,
        table: str,
        row: Mapping[str, Any],
    ) -> None:
        columns = _table_columns(connection, table)
        serialized = ItemProjectionState._serialize_row(row)
        selected = [column for column in columns if column in serialized]
        placeholders = ", ".join("?" for _ in selected)
        connection.execute(
            f"""
            INSERT INTO {table}({", ".join(selected)})
            VALUES({placeholders})
            """,
            tuple(serialized[column] for column in selected),
        )

    def persist(self, connection: sqlite3.Connection) -> None:
        for table in _ITEM_TABLE_DELETE_ORDER:
            if (
                table in _OPTIONAL_ITEM_PROJECTION_TABLES
                and not _table_columns(connection, table)
            ):
                continue
            connection.execute(f"DELETE FROM {table}")
        rows_by_table: tuple[
            tuple[str, Iterable[dict[str, Any]]], ...
        ] = (
            ("item_definitions", self.definitions.values()),
            ("item_instances", self.instances.values()),
            ("item_stacks", self.stacks.values()),
            ("item_function_definitions", self.functions.values()),
            ("item_function_bindings", self.bindings.values()),
            ("item_custody_state", self.custody.values()),
            ("item_runtime_state", self.runtime.values()),
            (
                "item_function_runtime_state",
                (
                    row
                    for row in self.function_runtime.values()
                    if str(row.get("subject_type") or "item_instance")
                    == "item_instance"
                ),
            ),
            (
                "item_stack_function_runtime_state",
                (
                    row
                    for row in self.function_runtime.values()
                    if str(row.get("subject_type") or "") == "item_stack"
                ),
            ),
            ("item_use_history", self.use_history.values()),
            (
                "item_observations",
                (
                    row
                    for row in self.observations.values()
                    if str(row.get("subject_type") or "")
                    in {"item_instance", "item_stack"}
                    and row.get("observer_entity_id") is not None
                ),
            ),
            (
                "item_knowledge_observations",
                (
                    row
                    for row in self.observations.values()
                    if str(row.get("subject_type") or "")
                    == "item_definition"
                    or row.get("observer_entity_id") is None
                ),
            ),
        )
        for table, rows in rows_by_table:
            if not _table_columns(connection, table):
                if table in _OPTIONAL_ITEM_PROJECTION_TABLES:
                    if any(True for _ in rows):
                        raise ContinuityError(
                            "ITEM_SCHEMA_UPGRADE_REQUIRED",
                            f"{table} is required for this item projection",
                            details={"table": table},
                        )
                    continue
                raise ContinuityError(
                    "ITEM_SCHEMA_INCOMPLETE",
                    f"missing item projection table: {table}",
                    details={"table": table},
                )
            for row in sorted(
                rows,
                key=lambda item: canonical_json(
                    {
                        key: value
                        for key, value in item.items()
                        if key not in _JSON_COLUMNS
                    }
                ),
            ):
                self._insert_row(connection, table, row)

    @staticmethod
    def _definition_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return dict(_decode_json(row.get("definition_json"), {}))

    @staticmethod
    def _instance_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return dict(_decode_json(row.get("instance_json"), {}))

    @staticmethod
    def _batch_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return dict(_decode_json(row.get("batch_json"), {}))

    @staticmethod
    def _binding_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return dict(_decode_json(row.get("binding_json"), {}))

    @staticmethod
    def _state_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return dict(_decode_json(row.get("state_json"), {}))

    @staticmethod
    def _story_coordinate(event: Mapping[str, Any]) -> dict[str, Any] | None:
        return _coordinate(event.get("story_coordinate"))

    @staticmethod
    def _require_story_coordinate(
        event: Mapping[str, Any],
    ) -> dict[str, Any]:
        coordinate = ItemProjectionState._story_coordinate(event)
        if coordinate is None:
            raise ContinuityError(
                "ITEM_STORY_COORDINATE_REQUIRED",
                "accepted item state changes require story_coordinate",
                details={
                    "event_type": event.get("event_type"),
                    "action": event.get("action"),
                },
            )
        return coordinate

    def _definition_for_subject(
        self,
        subject_type: str,
        subject_id: str,
    ) -> tuple[str, dict[str, Any]]:
        if subject_type == "item_instance":
            row = self.instances.get(subject_id)
            if row is None:
                raise ContinuityError(
                    "ITEM_INSTANCE_NOT_FOUND",
                    f"unknown item instance: {subject_id}",
                )
            if str(row.get("instance_status")) not in {
                "active",
                LEGACY_ITEM_SELF_INSTANCE,
            }:
                raise ContinuityError(
                    "ITEM_INSTANCE_INACTIVE",
                    "item instance is not active",
                    details={
                        "item_instance_id": subject_id,
                        "status": row.get("instance_status"),
                    },
                )
            definition_id = str(row["item_definition_id"])
        else:
            row = self.stacks.get(subject_id)
            if row is None:
                raise ContinuityError(
                    "ITEM_STACK_NOT_FOUND",
                    f"unknown item stack: {subject_id}",
                )
            if str(row.get("stack_status")) != "active":
                raise ContinuityError(
                    "ITEM_STACK_INACTIVE",
                    "item stack is not active",
                    details={
                        "stack_id": subject_id,
                        "status": row.get("stack_status"),
                    },
                )
            definition_id = str(row["item_definition_id"])
        definition = self.definitions.get(definition_id)
        if definition is None:
            raise ContinuityError(
                "ITEM_DEFINITION_NOT_FOUND",
                f"unknown item definition: {definition_id}",
            )
        return definition_id, definition

    @classmethod
    def _binding_enabled(cls, binding: Mapping[str, Any]) -> bool:
        if "enabled" in binding:
            return bool(binding.get("enabled"))
        payload = cls._binding_payload(binding)
        enabled = payload.get("enabled", True)
        return enabled is True

    @staticmethod
    def _binding_matches_subject(
        binding: Mapping[str, Any],
        *,
        subject_type: str,
        subject_id: str,
        definition_id: str,
    ) -> bool:
        return bool(
            binding.get("item_definition_id") == definition_id
            or (
                subject_type == "item_instance"
                and binding.get("item_instance_id") == subject_id
            )
            or (
                subject_type == "item_stack"
                and binding.get("stack_id") == subject_id
            )
        )

    def _function_binding_rows(
        self,
        subject_type: str,
        subject_id: str,
        definition_id: str,
        *,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        rows = [
            binding
            for binding in self.bindings.values()
            if self._binding_matches_subject(
                binding,
                subject_type=subject_type,
                subject_id=subject_id,
                definition_id=definition_id,
            )
            and (
                include_inactive
                or (
                    str(binding.get("binding_status")) == "active"
                    and self._binding_enabled(binding)
                )
            )
        ]
        return sorted(rows, key=lambda row: str(row.get("binding_id") or ""))

    def _active_function_bindings(
        self,
        subject_type: str,
        subject_id: str,
        definition_id: str,
    ) -> set[str]:
        result = {
            str(value)
            for value in (
            self._definition_payload(self.definitions[definition_id]).get(
                "default_functions"
            )
            or []
            )
        }
        for binding in self._function_binding_rows(
            subject_type,
            subject_id,
            definition_id,
        ):
            result.add(str(binding["function_id"]))
        return result

    def _function_for_subject(
        self,
        subject_type: str,
        subject_id: str,
        function_id: str,
        *,
        require_active_binding: bool = True,
    ) -> dict[str, Any]:
        definition_id, _ = self._definition_for_subject(
            subject_type, subject_id
        )
        function = self.functions.get(function_id)
        if (
            function is None
            or str(function.get("function_status")) != "active"
        ):
            raise ContinuityError(
                "ITEM_FUNCTION_NOT_FOUND",
                f"unknown or inactive item function: {function_id}",
            )
        if str(function["item_definition_id"]) != definition_id:
            raise ContinuityError(
                "ITEM_FUNCTION_DEFINITION_MISMATCH",
                "item function belongs to a different definition",
                details={
                    "function_id": function_id,
                    "subject_definition_id": definition_id,
                    "function_definition_id": function.get(
                        "item_definition_id"
                    ),
                },
            )
        active = self._active_function_bindings(
            subject_type, subject_id, definition_id
        )
        if require_active_binding:
            bound = function_id in active
        else:
            default_functions = {
                str(value)
                for value in (
                    self._definition_payload(
                        self.definitions[definition_id]
                    ).get("default_functions")
                    or []
                )
            }
            known_bindings = {
                str(binding.get("function_id") or "")
                for binding in self._function_binding_rows(
                    subject_type,
                    subject_id,
                    definition_id,
                    include_inactive=True,
                )
            }
            bound = function_id in default_functions | known_bindings
        if not bound:
            raise ContinuityError(
                "ITEM_FUNCTION_NOT_BOUND",
                "item function is not bound to the addressed item",
                details={
                    "function_id": function_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                },
            )
        return function

    def _ensure_function_runtime(
        self,
        subject_type: str,
        subject_id: str,
        function_id: str,
        *,
        source_event_id: str,
        updated_order: int,
        coordinate: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        key = _function_runtime_key(subject_type, subject_id, function_id)
        row = self.function_runtime.get(key)
        if row is not None:
            return row
        function = self.functions.get(function_id)
        definition = (
            self._definition_payload(function) if function is not None else {}
        )
        remaining = definition.get("charges")
        definition_id, _ = self._definition_for_subject(
            subject_type, subject_id
        )
        enabled = function_id in self._active_function_bindings(
            subject_type,
            subject_id,
            definition_id,
        )
        unlock_state = str(
            definition.get("initial_unlock_state") or "unlocked"
        )
        if unlock_state not in ITEM_FUNCTION_UNLOCK_STATES:
            raise ContinuityError(
                "ITEM_INVALID_ENUM",
                "function.initial_unlock_state is not supported",
                details={
                    "field": "function.initial_unlock_state",
                    "value": unlock_state,
                    "choices": sorted(ITEM_FUNCTION_UNLOCK_STATES),
                },
            )
        row = {
            "function_runtime_key": stable_item_id(
                "item_function_runtime_",
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "function_id": function_id,
                },
            ),
            "subject_type": subject_type,
            "subject_id": subject_id,
            "item_instance_id": (
                subject_id if subject_type == "item_instance" else None
            ),
            "stack_id": (
                subject_id if subject_type == "item_stack" else None
            ),
            "function_id": function_id,
            "enabled": int(enabled and unlock_state == "unlocked"),
            "unlock_state": unlock_state,
            "remaining_charges": (
                _finite_number(
                    remaining,
                    field="function.remaining_charges",
                    minimum=0,
                )
                if remaining is not None
                else None
            ),
            "cooldown_until_json": None,
            "state_json": {},
            "source_event_id": source_event_id,
            "story_coordinate_json": dict(coordinate or {}),
            "updated_order": int(updated_order),
        }
        self.function_runtime[key] = row
        return row

    def _ensure_instance_runtime(
        self,
        item_instance_id: str,
        *,
        source_event_id: str,
        updated_order: int,
        coordinate: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        row = self.runtime.get(item_instance_id)
        if row is not None:
            return row
        instance = self.instances[item_instance_id]
        definition = self._definition_payload(
            self.definitions[str(instance["item_definition_id"])]
        )
        max_durability = definition.get("max_durability")
        max_energy = definition.get("max_energy")
        row = {
            "item_instance_id": item_instance_id,
            "durability": (
                _finite_number(
                    max_durability,
                    field="item.max_durability",
                    minimum=0,
                )
                if max_durability is not None
                else None
            ),
            "max_durability": (
                _finite_number(
                    max_durability,
                    field="item.max_durability",
                    minimum=0,
                )
                if max_durability is not None
                else None
            ),
            "energy": (
                _finite_number(
                    max_energy,
                    field="item.max_energy",
                    minimum=0,
                )
                if max_energy is not None
                else None
            ),
            "max_energy": (
                _finite_number(
                    max_energy,
                    field="item.max_energy",
                    minimum=0,
                )
                if max_energy is not None
                else None
            ),
            "sealed": 0,
            "damaged": 0,
            "destroyed": 0,
            "active": 0,
            "equipped_by_entity_id": None,
            "slot_key": None,
            "bound_actor_entity_id": None,
            "state_json": {},
            "source_event_id": source_event_id,
            "story_coordinate_json": dict(coordinate or {}),
            "updated_order": int(updated_order),
        }
        self.runtime[item_instance_id] = row
        for function_id in sorted(
            self._active_function_bindings(
                "item_instance",
                item_instance_id,
                str(instance["item_definition_id"]),
            )
        ):
            if function_id in self.functions:
                self._ensure_function_runtime(
                    "item_instance",
                    item_instance_id,
                    function_id,
                    source_event_id=source_event_id,
                    updated_order=updated_order,
                    coordinate=coordinate,
                )
        return row

    @staticmethod
    def _unique_string_list(value: Any, *, field: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ContinuityError(
                "ITEM_INVALID_COLLECTION",
                f"{field} must be a list of non-empty strings",
                details={"field": field},
            )
        result: list[str] = []
        seen: set[str] = set()
        for raw in value:
            if not isinstance(raw, str) or not raw.strip():
                raise ContinuityError(
                    "ITEM_INVALID_COLLECTION",
                    f"{field} must be a list of non-empty strings",
                    details={"field": field, "value": raw},
                )
            item = raw.strip()
            if item in seen:
                raise ContinuityError(
                    "ITEM_UNIQUENESS_VIOLATION",
                    f"{field} cannot contain duplicate identifiers",
                    details={"field": field, "value": item},
                )
            seen.add(item)
            result.append(item)
        return result

    @staticmethod
    def _predicate_mappings(
        value: Any,
        *,
        field: str,
    ) -> list[dict[str, Any]]:
        if value in (None, {}, []):
            return []
        if isinstance(value, Mapping):
            return [dict(value)]
        if isinstance(value, list) and all(
            isinstance(item, Mapping) for item in value
        ):
            return [dict(item) for item in value]
        raise ContinuityError(
            "ITEM_CONDITION_INVALID",
            f"{field} must be an object or list of objects",
            details={"field": field},
        )

    @staticmethod
    def _activation_kind(definition: Mapping[str, Any]) -> str:
        activation = definition.get("activation")
        if isinstance(activation, Mapping):
            value = (
                definition.get("activation_kind")
                or activation.get("kind")
                or activation.get("activation_kind")
                or "active"
            )
        elif isinstance(activation, str):
            value = definition.get("activation_kind") or activation
        else:
            value = definition.get("activation_kind") or "active"
        return _require_enum(
            value,
            field="function.activation_kind",
            choices=ITEM_ACTIVATION_KINDS,
        )

    @staticmethod
    def _validate_target_spec(value: Any) -> list[Any]:
        if value in (None, []):
            return []
        if not isinstance(value, list):
            raise ContinuityError(
                "ITEM_TARGET_SPEC_INVALID",
                "function.targets must be a list",
                details={"field": "function.targets"},
            )
        normalized: list[Any] = []
        seen: set[str] = set()
        for index, raw in enumerate(value):
            if isinstance(raw, str):
                kind = raw.strip()
                if not kind:
                    raise ContinuityError(
                        "ITEM_TARGET_SPEC_INVALID",
                        "function target kind cannot be empty",
                        details={"index": index},
                    )
                item: Any = kind
                identity = canonical_json(item)
            elif isinstance(raw, Mapping):
                item = dict(raw)
                kind = str(
                    item.get("kind") or item.get("entity_type") or ""
                ).strip()
                if not kind:
                    raise ContinuityError(
                        "ITEM_TARGET_SPEC_INVALID",
                        "function target object requires kind or entity_type",
                        details={"index": index},
                    )
                item["kind"] = kind
                if "required" in item:
                    item["required"] = _require_boolean(
                        item["required"],
                        field=f"function.targets[{index}].required",
                    )
                if item.get("max_count") is not None:
                    maximum = _finite_number(
                        item["max_count"],
                        field=f"function.targets[{index}].max_count",
                        minimum=0,
                    )
                    if not maximum.is_integer() or maximum < 1:
                        raise ContinuityError(
                            "ITEM_TARGET_SPEC_INVALID",
                            "target max_count must be a positive whole number",
                            details={
                                "field": f"function.targets[{index}].max_count"
                            },
                        )
                    item["max_count"] = int(maximum)
                identity = canonical_json(item)
            else:
                raise ContinuityError(
                    "ITEM_TARGET_SPEC_INVALID",
                    "function target entries must be strings or objects",
                    details={"index": index},
                )
            if identity in seen:
                raise ContinuityError(
                    "ITEM_UNIQUENESS_VIOLATION",
                    "function.targets cannot contain duplicate entries",
                    details={"index": index},
                )
            seen.add(identity)
            normalized.append(item)
        return normalized

    @staticmethod
    def _validate_range_spec(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            return _require_enum(
                value,
                field="function.range",
                choices=ITEM_RANGE_KINDS,
            )
        if isinstance(value, bool):
            raise ContinuityError(
                "ITEM_RANGE_SPEC_INVALID",
                "function.range cannot be boolean",
            )
        if isinstance(value, (int, float)):
            return {
                "kind": "distance",
                "max_distance": _finite_number(
                    value,
                    field="function.range.max_distance",
                    minimum=0,
                ),
            }
        if not isinstance(value, Mapping):
            raise ContinuityError(
                "ITEM_RANGE_SPEC_INVALID",
                "function.range must be a supported kind, number, or object",
            )
        result = dict(value)
        result["kind"] = _require_enum(
            result.get("kind") or "distance",
            field="function.range.kind",
            choices=ITEM_RANGE_KINDS,
        )
        if result.get("max_distance") is not None:
            result["max_distance"] = _finite_number(
                result["max_distance"],
                field="function.range.max_distance",
                minimum=0,
            )
        if result.get("location_entity_ids") is not None:
            result["location_entity_ids"] = (
                ItemProjectionState._unique_string_list(
                    result["location_entity_ids"],
                    field="function.range.location_entity_ids",
                )
            )
        return result

    @classmethod
    def _preflight_item_definition(
        cls,
        definition: dict[str, Any],
    ) -> None:
        item_kind = str(definition.get("item_kind") or "miscellaneous").strip()
        if not item_kind:
            raise ContinuityError(
                "ITEM_INVALID_ENUM",
                "definition.item_kind cannot be empty",
                details={"field": "definition.item_kind"},
            )
        definition["item_kind"] = item_kind
        definition["stack_policy"] = _require_enum(
            definition.get("stack_policy") or "non_stackable",
            field="definition.stack_policy",
            choices=ITEM_STACK_POLICIES,
        )
        definition["uniqueness_policy"] = _require_enum(
            definition.get("uniqueness_policy") or "ordinary",
            field="definition.uniqueness_policy",
            choices=ITEM_UNIQUENESS_POLICIES,
        )
        for field in (
            "capacity",
            "unit_bulk",
            "max_durability",
            "max_energy",
        ):
            if definition.get(field) is not None:
                definition[field] = _finite_number(
                    definition[field],
                    field=f"definition.{field}",
                    minimum=0,
                )
        definition["default_functions"] = cls._unique_string_list(
            definition.get("default_functions"),
            field="definition.default_functions",
        )

    @classmethod
    def _preflight_function_definition(
        cls,
        definition: dict[str, Any],
    ) -> None:
        definition["effect_owner"] = _require_enum(
            definition.get("effect_owner") or "inline",
            field="function.effect_owner",
            choices={"inline", "ability_bridge"},
        )
        definition["activation_kind"] = cls._activation_kind(definition)
        if isinstance(definition.get("activation"), Mapping):
            activation = dict(definition["activation"])
            for field in (
                "requires_active",
                "requires_equipped",
                "requires_bound",
            ):
                if field in activation:
                    activation[field] = _require_boolean(
                        activation[field],
                        field=f"function.activation.{field}",
                    )
            if activation.get("conditions") is not None:
                cls._predicate_mappings(
                    activation["conditions"],
                    field="function.activation.conditions",
                )
            definition["activation"] = activation
        elif definition.get("activation") is not None and not isinstance(
            definition.get("activation"), str
        ):
            raise ContinuityError(
                "ITEM_ACTIVATION_SPEC_INVALID",
                "function.activation must be a string or object",
            )
        for field in ("charges", "durability_cost", "capacity"):
            if definition.get(field) is not None:
                definition[field] = _finite_number(
                    definition[field],
                    field=f"function.{field}",
                    minimum=0,
                )
        if definition.get("cooldown") is not None:
            cooldown = _finite_number(
                definition["cooldown"],
                field="function.cooldown",
                minimum=0,
            )
            if not cooldown.is_integer():
                raise ContinuityError(
                    "ITEM_INVALID_COOLDOWN",
                    "function.cooldown must be a whole story-coordinate delta",
                )
            definition["cooldown"] = int(cooldown)
        costs = definition.get("costs") or []
        if isinstance(costs, Mapping):
            costs = [
                {"kind": key, "amount": value}
                for key, value in costs.items()
            ]
        if not isinstance(costs, list):
            raise ContinuityError(
                "ITEM_COST_SPEC_INVALID",
                "function.costs must be a list or object",
            )
        normalized_costs: list[Any] = []
        for index, cost in enumerate(costs):
            if not isinstance(cost, Mapping):
                raise ContinuityError(
                    "ITEM_COST_SPEC_INVALID",
                    "function cost entries must be objects",
                    details={"index": index},
                )
            normalized = dict(cost)
            kind = str(
                normalized.get("kind")
                or normalized.get("resource")
                or ""
            ).strip()
            if not kind:
                raise ContinuityError(
                    "ITEM_COST_SPEC_INVALID",
                    "function cost requires kind or resource",
                    details={"index": index},
                )
            normalized["kind"] = kind
            if normalized.get("amount") is not None:
                normalized["amount"] = _finite_number(
                    normalized["amount"],
                    field=f"function.costs[{index}].amount",
                    minimum=0,
                )
            normalized_costs.append(normalized)
        definition["costs"] = normalized_costs
        definition["targets"] = cls._validate_target_spec(
            definition.get("targets") or []
        )
        definition["range"] = cls._validate_range_spec(
            definition.get("range")
        )
        cls._predicate_mappings(
            definition.get("conditions"),
            field="function.conditions",
        )
        cls._predicate_mappings(
            definition.get("prerequisites"),
            field="function.prerequisites",
        )
        definition["granted_ability_ids"] = cls._unique_string_list(
            definition.get("granted_ability_ids"),
            field="function.granted_ability_ids",
        )
        if definition.get("initial_unlock_state") is not None:
            definition["initial_unlock_state"] = _require_enum(
                definition["initial_unlock_state"],
                field="function.initial_unlock_state",
                choices=ITEM_FUNCTION_UNLOCK_STATES,
            )

    @classmethod
    def _preflight_binding_definition(
        cls,
        definition: dict[str, Any],
    ) -> None:
        if "enabled" in definition:
            definition["enabled"] = _require_boolean(
                definition["enabled"],
                field="binding.enabled",
            )
        else:
            definition["enabled"] = True
        definition["binding_status"] = _require_enum(
            definition.get("binding_status")
            or definition.get("status")
            or "active",
            field="binding.binding_status",
            choices=ITEM_BINDING_STATUSES,
        )
        cls._predicate_mappings(
            definition.get("conditions"),
            field="binding.conditions",
        )

    def _subjects_for_binding(
        self,
        binding: Mapping[str, Any],
    ) -> list[tuple[str, str]]:
        if binding.get("item_instance_id"):
            return [("item_instance", str(binding["item_instance_id"]))]
        if binding.get("stack_id"):
            return [("item_stack", str(binding["stack_id"]))]
        definition_id = str(binding.get("item_definition_id") or "")
        subjects = [
            ("item_instance", instance_id)
            for instance_id, instance in self.instances.items()
            if str(instance.get("item_definition_id")) == definition_id
            and str(instance.get("instance_status"))
            in {"active", LEGACY_ITEM_SELF_INSTANCE}
        ]
        subjects.extend(
            ("item_stack", stack_id)
            for stack_id, stack in self.stacks.items()
            if str(stack.get("item_definition_id")) == definition_id
            and str(stack.get("stack_status")) == "active"
        )
        return sorted(subjects)

    def _set_function_runtime_unavailable(
        self,
        subject_type: str,
        subject_id: str,
        function_id: str,
        *,
        reason: str,
        source_event_id: str,
        updated_order: int,
        coordinate: Mapping[str, Any] | None = None,
    ) -> None:
        row = self.function_runtime.get(
            _function_runtime_key(subject_type, subject_id, function_id)
        )
        if row is None:
            return
        row.update(
            {
                "enabled": 0,
                "unlock_state": "suppressed",
                "source_event_id": source_event_id,
                "story_coordinate_json": dict(coordinate or {}),
                "updated_order": int(updated_order),
                "state_json": {
                    **self._state_payload(row),
                    "suppressed_by": reason,
                },
            }
        )

    def _refresh_binding_runtime_availability(
        self,
        binding: Mapping[str, Any],
        *,
        reason: str,
        source_event_id: str,
        updated_order: int,
    ) -> None:
        function_id = str(binding.get("function_id") or "")
        for subject_type, subject_id in self._subjects_for_binding(binding):
            try:
                definition_id, _ = self._definition_for_subject(
                    subject_type, subject_id
                )
            except ContinuityError:
                definition_id = ""
            active = (
                bool(definition_id)
                and function_id
                in self._active_function_bindings(
                    subject_type, subject_id, definition_id
                )
            )
            if not active:
                self._set_function_runtime_unavailable(
                    subject_type,
                    subject_id,
                    function_id,
                    reason=reason,
                    source_event_id=source_event_id,
                    updated_order=updated_order,
                )

    def _apply_item_spec(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str,
        updated_order: int,
    ) -> None:
        action = str(event.get("action") or "define")
        spec_type = str(event.get("spec_type") or "")
        spec_id = str(event.get("spec_id") or "").strip()
        definition = dict(event.get("definition") or {})
        supersedes = str(event.get("supersedes_spec_id") or "").strip()
        if spec_type == "item_definition":
            target = self.definitions
            status_field = "item_status"
        elif spec_type == "function_definition":
            target = self.functions
            status_field = "function_status"
        elif spec_type == "function_binding":
            target = self.bindings
            status_field = "binding_status"
        else:
            raise ContinuityError(
                "ITEM_SPEC_TYPE_UNSUPPORTED",
                f"unsupported item spec type: {spec_type}",
            )

        if action == "deprecate":
            existing = target.get(spec_id)
            if existing is None:
                raise ContinuityError(
                    "ITEM_SPEC_NOT_FOUND",
                    "item spec deprecation requires an accepted definition",
                    details={"spec_type": spec_type, "spec_id": spec_id},
                )
            existing[status_field] = "deprecated"
            existing["source_event_id"] = source_event_id
            existing["updated_order"] = int(updated_order)
            if spec_type == "function_binding":
                self._refresh_binding_runtime_availability(
                    existing,
                    reason="binding_deprecated",
                    source_event_id=source_event_id,
                    updated_order=updated_order,
                )
            elif spec_type == "function_definition":
                for subject_type, subject_id, function_id in sorted(
                    self.function_runtime
                ):
                    if function_id == spec_id:
                        self._set_function_runtime_unavailable(
                            subject_type,
                            subject_id,
                            function_id,
                            reason="function_deprecated",
                            source_event_id=source_event_id,
                            updated_order=updated_order,
                        )
                for binding in self.bindings.values():
                    if str(binding.get("function_id") or "") == spec_id:
                        binding["binding_status"] = "deprecated"
                        binding["source_event_id"] = source_event_id
                        binding["updated_order"] = int(updated_order)
            return

        if action == "supersede":
            previous = target.get(supersedes)
            if previous is None:
                raise ContinuityError(
                    "ITEM_SPEC_NOT_FOUND",
                    "item spec supersession target does not exist",
                    details={
                        "spec_type": spec_type,
                        "spec_id": spec_id,
                        "supersedes_spec_id": supersedes,
                    },
                )
            previous[status_field] = "superseded"
            previous["updated_order"] = int(updated_order)
            if spec_type == "function_definition":
                for subject_type, subject_id, function_id in sorted(
                    self.function_runtime
                ):
                    if function_id == supersedes:
                        self._set_function_runtime_unavailable(
                            subject_type,
                            subject_id,
                            function_id,
                            reason="function_superseded",
                            source_event_id=source_event_id,
                            updated_order=updated_order,
                        )
                for binding in self.bindings.values():
                    if str(binding.get("function_id") or "") == supersedes:
                        binding["binding_status"] = "superseded"
                        binding["source_event_id"] = source_event_id
                        binding["updated_order"] = int(updated_order)

        existing = target.get(spec_id)
        if (
            existing is not None
            and str(existing.get(status_field)) == "active"
            and action == "define"
        ):
            previous_definition = (
                self._definition_payload(existing)
                if spec_type != "function_binding"
                else self._binding_payload(existing)
            )
            if previous_definition != definition:
                raise ContinuityError(
                    "ITEM_SPEC_ALREADY_EXISTS",
                    "an active item spec id is immutable; use supersede",
                    details={"spec_type": spec_type, "spec_id": spec_id},
                )
            return

        if spec_type == "item_definition":
            self._preflight_item_definition(definition)
            item_entity_id = event.get("item_entity_id") or definition.get(
                "item_entity_id"
            )
            if item_entity_id:
                legacy_ids = [
                    definition_id
                    for definition_id, row in self.definitions.items()
                    if row.get("item_entity_id") == str(item_entity_id)
                    and definition_id != spec_id
                    and str(row.get("item_status"))
                    in {LEGACY_ITEM_UNMODELED, LEGACY_ITEM_SELF_INSTANCE}
                ]
                strong_conflicts = [
                    definition_id
                    for definition_id, row in self.definitions.items()
                    if row.get("item_entity_id") == str(item_entity_id)
                    and definition_id != spec_id
                    and str(row.get("item_status"))
                    not in {LEGACY_ITEM_UNMODELED, LEGACY_ITEM_SELF_INSTANCE}
                ]
                if strong_conflicts:
                    raise ContinuityError(
                        "ITEM_ENTITY_DEFINITION_CONFLICT",
                        "item entity already belongs to a typed item definition",
                        details={
                            "item_entity_id": str(item_entity_id),
                            "item_definition_ids": strong_conflicts,
                        },
                    )
                for legacy_id in legacy_ids:
                    self.definitions.pop(legacy_id, None)
                    for instance in self.instances.values():
                        if str(instance.get("item_definition_id")) == legacy_id:
                            instance["item_definition_id"] = spec_id
                            payload = self._instance_payload(instance)
                            payload["item_definition_id"] = spec_id
                            payload["modeling_status"] = "typed"
                            instance["instance_json"] = payload
            definition.setdefault("item_definition_id", spec_id)
            target[spec_id] = {
                "item_definition_id": spec_id,
                "item_entity_id": (
                    str(item_entity_id) if item_entity_id else None
                ),
                "item_status": "active",
                "item_kind": str(definition["item_kind"]),
                "stack_policy": str(definition["stack_policy"]),
                "uniqueness_policy": str(
                    definition["uniqueness_policy"]
                ),
                "definition_json": definition,
                "source_event_id": source_event_id,
                "updated_order": int(updated_order),
            }
            return

        if spec_type == "function_definition":
            self._preflight_function_definition(definition)
            item_definition_id = str(
                definition.get("item_definition_id")
                or event.get("item_definition_id")
                or ""
            )
            item_definition = self.definitions.get(item_definition_id)
            if (
                item_definition is None
                or str(item_definition.get("item_status")) != "active"
            ):
                raise ContinuityError(
                    "ITEM_DEFINITION_NOT_FOUND",
                    "item function requires an active item definition",
                    details={"item_definition_id": item_definition_id},
                )
            effect_owner = str(definition["effect_owner"])
            granted = set(definition["granted_ability_ids"])
            if effect_owner == "inline" and granted:
                raise ContinuityError(
                    "ITEM_ABILITY_BRIDGE_DUPLICATE",
                    "inline item functions cannot also grant bridged abilities",
                    details={
                        "function_id": spec_id,
                        "granted_ability_ids": sorted(granted),
                    },
                )
            if effect_owner == "ability_bridge":
                if not self.rollout_policy.power_binding_bridge:
                    raise ContinuityError(
                        "ITEM_POWER_BINDING_BRIDGE_DISABLED",
                        "item-to-ability bridge rollout is disabled",
                        details={"function_id": spec_id},
                    )
                if not granted:
                    raise ContinuityError(
                        "ITEM_ABILITY_BRIDGE_REQUIRED",
                        "ability_bridge item functions require granted_ability_ids",
                        details={"function_id": spec_id},
                    )
                missing = sorted(
                    granted - self.active_ability_definitions
                )
                if missing:
                    raise ContinuityError(
                        "ITEM_ABILITY_DEFINITION_NOT_FOUND",
                        "ability bridge references a missing accepted ability definition",
                        details={"ability_entity_ids": missing},
                    )
                duplicated = [
                    key
                    for key in (
                        "inline_effects",
                        "costs",
                        "cooldown",
                        "counters",
                    )
                    if definition.get(key)
                ]
                if duplicated:
                    raise ContinuityError(
                        "ITEM_ABILITY_BRIDGE_DUPLICATE",
                        "ability bridge cannot duplicate ability effects, costs, cooldowns, or counters",
                        details={"fields": duplicated},
                    )
            definition.setdefault("function_id", spec_id)
            target[spec_id] = {
                "function_id": spec_id,
                "item_definition_id": item_definition_id,
                "function_status": "active",
                "effect_owner": effect_owner,
                "definition_json": definition,
                "source_event_id": source_event_id,
                "updated_order": int(updated_order),
            }
            return

        function_id = str(
            definition.get("function_id")
            or event.get("function_id")
            or ""
        )
        function = self.functions.get(function_id)
        if (
            function is None
            or str(function.get("function_status")) != "active"
        ):
            raise ContinuityError(
                "ITEM_FUNCTION_NOT_FOUND",
                "item function binding requires an active function",
                details={"function_id": function_id},
            )
        self._preflight_binding_definition(definition)
        definition_id = str(
            definition.get("item_definition_id")
            or event.get("item_definition_id")
            or ""
        )
        instance_id = str(
            definition.get("item_instance_id")
            or event.get("item_instance_id")
            or ""
        )
        stack_id = str(
            definition.get("stack_id") or event.get("stack_id") or ""
        )
        populated = [
            bool(definition_id),
            bool(instance_id),
            bool(stack_id),
        ]
        if sum(populated) != 1:
            raise ContinuityError(
                "ITEM_BINDING_TARGET_REQUIRED",
                "item function binding requires exactly one definition, instance, or stack target",
            )
        if definition_id:
            if definition_id not in self.definitions:
                raise ContinuityError(
                    "ITEM_DEFINITION_NOT_FOUND",
                    f"unknown item definition: {definition_id}",
                )
            subject_definition_id = definition_id
        elif instance_id:
            instance = self.instances.get(instance_id)
            if instance is None:
                raise ContinuityError(
                    "ITEM_INSTANCE_NOT_FOUND",
                    f"unknown item instance: {instance_id}",
                )
            subject_definition_id = str(instance["item_definition_id"])
        else:
            stack = self.stacks.get(stack_id)
            if stack is None:
                raise ContinuityError(
                    "ITEM_STACK_NOT_FOUND",
                    f"unknown item stack: {stack_id}",
                )
            subject_definition_id = str(stack["item_definition_id"])
        if subject_definition_id != str(function["item_definition_id"]):
            raise ContinuityError(
                "ITEM_FUNCTION_DEFINITION_MISMATCH",
                "binding target and function use different item definitions",
                details={
                    "function_id": function_id,
                    "target_definition_id": subject_definition_id,
                    "function_definition_id": function.get(
                        "item_definition_id"
                    ),
                },
            )
        binding_status = str(definition["binding_status"])
        duplicate_bindings = sorted(
            str(binding_id)
            for binding_id, binding in self.bindings.items()
            if binding_id != spec_id
            and str(binding.get("binding_status")) == "active"
            and binding_status == "active"
            and str(binding.get("function_id") or "") == function_id
            and binding.get("item_definition_id") == (definition_id or None)
            and binding.get("item_instance_id") == (instance_id or None)
            and binding.get("stack_id") == (stack_id or None)
        )
        if duplicate_bindings:
            raise ContinuityError(
                "ITEM_BINDING_UNIQUENESS_CONFLICT",
                "an active function binding already exists for this target",
                details={
                    "function_id": function_id,
                    "binding_ids": duplicate_bindings,
                },
            )
        definition.setdefault("binding_id", spec_id)
        definition["function_id"] = function_id
        target[spec_id] = {
            "binding_id": spec_id,
            "item_definition_id": definition_id or None,
            "item_instance_id": instance_id or None,
            "stack_id": stack_id or None,
            "function_id": function_id,
            "binding_status": binding_status,
            "enabled": int(bool(definition["enabled"])),
            "binding_json": definition,
            "source_event_id": source_event_id,
            "updated_order": int(updated_order),
        }
        affected_subjects: list[tuple[str, str]] = []
        if instance_id:
            affected_subjects.append(("item_instance", instance_id))
        elif stack_id:
            affected_subjects.append(("item_stack", stack_id))
        elif definition_id:
            affected_subjects.extend(
                ("item_instance", instance_key)
                for instance_key, instance in self.instances.items()
                if str(instance["item_definition_id"]) == definition_id
                and str(instance.get("instance_status")) == "active"
            )
            affected_subjects.extend(
                ("item_stack", stack_key)
                for stack_key, stack in self.stacks.items()
                if str(stack["item_definition_id"]) == definition_id
                and str(stack.get("stack_status")) == "active"
            )
        if binding_status == "active" and bool(definition["enabled"]):
            for subject_type, affected in sorted(affected_subjects):
                runtime_row = self._ensure_function_runtime(
                    subject_type,
                    affected,
                    function_id,
                    source_event_id=source_event_id,
                    updated_order=updated_order,
                    coordinate=None,
                )
                runtime_state = self._state_payload(runtime_row)
                suppressed_by = str(
                    runtime_state.get("suppressed_by") or ""
                )
                if suppressed_by in {
                    "binding_deprecated",
                    "binding_superseded",
                }:
                    runtime_row["unlock_state"] = "unlocked"
                    runtime_state.pop("suppressed_by", None)
                    runtime_row["state_json"] = runtime_state
                if (
                    str(runtime_row.get("unlock_state")) == "unlocked"
                    and runtime_state.get("last_action")
                    not in {"bootstrap", "disable", "lock", "suppress"}
                ):
                    runtime_row["enabled"] = 1
        if action == "supersede":
            self._refresh_binding_runtime_availability(
                previous,
                reason="binding_superseded",
                source_event_id=source_event_id,
                updated_order=updated_order,
            )

    @staticmethod
    def _later_coordinate(
        left: Mapping[str, Any] | None,
        right: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if left is None:
            return dict(right) if right is not None else None
        if right is None:
            return dict(left)
        if str(left.get("calendar_id")) != str(right.get("calendar_id")):
            raise ContinuityError(
                "ITEM_STORY_COORDINATE_CONFLICT",
                "stack function runtimes use different story calendars",
                details={"left": dict(left), "right": dict(right)},
            )
        return dict(
            left
            if int(left.get("ordinal") or 0) >= int(right.get("ordinal") or 0)
            else right
        )

    def _clone_stack_bindings(
        self,
        source_stack_id: str,
        target_stack_id: str,
        *,
        source_event_id: str,
        updated_order: int,
    ) -> None:
        source_bindings = [
            deepcopy(binding)
            for binding in self.bindings.values()
            if binding.get("stack_id") == source_stack_id
            and str(binding.get("binding_status")) == "active"
        ]
        for binding in source_bindings:
            function_id = str(binding.get("function_id") or "")
            duplicate = any(
                candidate.get("stack_id") == target_stack_id
                and str(candidate.get("function_id") or "") == function_id
                and str(candidate.get("binding_status")) == "active"
                for candidate in self.bindings.values()
            )
            if duplicate:
                continue
            source_binding_id = str(binding.get("binding_id") or "")
            binding_id = stable_item_id(
                "item_binding_split_",
                {
                    "source_binding_id": source_binding_id,
                    "target_stack_id": target_stack_id,
                },
            )
            payload = self._binding_payload(binding)
            payload["binding_id"] = binding_id
            payload["stack_id"] = target_stack_id
            payload.pop("item_instance_id", None)
            payload.pop("item_definition_id", None)
            binding.update(
                {
                    "binding_id": binding_id,
                    "item_definition_id": None,
                    "item_instance_id": None,
                    "stack_id": target_stack_id,
                    "binding_json": payload,
                    "source_event_id": source_event_id,
                    "updated_order": int(updated_order),
                }
            )
            self.bindings[binding_id] = binding

    def _split_stack_function_runtime(
        self,
        source_stack_id: str,
        target_stack_id: str,
        *,
        split_quantity: float,
        source_quantity_before: float,
        coordinate: Mapping[str, Any],
        source_event_id: str,
        updated_order: int,
    ) -> None:
        ratio = split_quantity / source_quantity_before
        self._clone_stack_bindings(
            source_stack_id,
            target_stack_id,
            source_event_id=source_event_id,
            updated_order=updated_order,
        )
        source_rows = [
            (key, row)
            for key, row in self.function_runtime.items()
            if key[0] == "item_stack" and key[1] == source_stack_id
        ]
        for (_, _, function_id), row in source_rows:
            target_key = _function_runtime_key(
                "item_stack", target_stack_id, function_id
            )
            target_row = deepcopy(row)
            target_row.update(
                {
                    "function_runtime_key": stable_item_id(
                        "item_function_runtime_",
                        {
                            "subject_type": "item_stack",
                            "subject_id": target_stack_id,
                            "function_id": function_id,
                        },
                    ),
                    "subject_type": "item_stack",
                    "subject_id": target_stack_id,
                    "item_instance_id": None,
                    "stack_id": target_stack_id,
                    "source_event_id": source_event_id,
                    "story_coordinate_json": dict(coordinate),
                    "updated_order": int(updated_order),
                    "state_json": {
                        **self._state_payload(row),
                        "split_from_stack_id": source_stack_id,
                    },
                }
            )
            remaining = row.get("remaining_charges")
            if remaining is not None:
                target_remaining = _finite_number(
                    float(remaining) * ratio,
                    field="function.remaining_charges",
                    minimum=0,
                )
                source_remaining = _finite_number(
                    float(remaining) - target_remaining,
                    field="function.remaining_charges",
                    minimum=0,
                )
                target_row["remaining_charges"] = target_remaining
                row["remaining_charges"] = source_remaining
            row.update(
                {
                    "source_event_id": source_event_id,
                    "story_coordinate_json": dict(coordinate),
                    "updated_order": int(updated_order),
                    "state_json": {
                        **self._state_payload(row),
                        "split_to_stack_id": target_stack_id,
                    },
                }
            )
            self.function_runtime[target_key] = target_row
        definition_id = str(
            self.stacks[target_stack_id]["item_definition_id"]
        )
        for function_id in sorted(
            self._active_function_bindings(
                "item_stack", target_stack_id, definition_id
            )
        ):
            if (
                _function_runtime_key(
                    "item_stack", target_stack_id, function_id
                )
                not in self.function_runtime
                and function_id in self.functions
            ):
                self._ensure_function_runtime(
                    "item_stack",
                    target_stack_id,
                    function_id,
                    source_event_id=source_event_id,
                    updated_order=updated_order,
                    coordinate=coordinate,
                )

    def _merge_stack_function_runtime(
        self,
        source_stack_id: str,
        target_stack_id: str,
        *,
        moved_quantity: float,
        source_quantity_before: float,
        coordinate: Mapping[str, Any],
        source_event_id: str,
        updated_order: int,
    ) -> None:
        ratio = moved_quantity / source_quantity_before
        source_depleted = math.isclose(
            moved_quantity,
            source_quantity_before,
            rel_tol=0.0,
            abs_tol=_EPSILON,
        )
        self._clone_stack_bindings(
            source_stack_id,
            target_stack_id,
            source_event_id=source_event_id,
            updated_order=updated_order,
        )
        source_rows = [
            (key, row)
            for key, row in self.function_runtime.items()
            if key[0] == "item_stack" and key[1] == source_stack_id
        ]
        priority = {"unlocked": 0, "locked": 1, "suppressed": 2}
        for (_, _, function_id), source_row in source_rows:
            target_key = _function_runtime_key(
                "item_stack", target_stack_id, function_id
            )
            target_row = self.function_runtime.get(target_key)
            if target_row is None:
                target_row = deepcopy(source_row)
                target_row.update(
                    {
                        "function_runtime_key": stable_item_id(
                            "item_function_runtime_",
                            {
                                "subject_type": "item_stack",
                                "subject_id": target_stack_id,
                                "function_id": function_id,
                            },
                        ),
                        "subject_type": "item_stack",
                        "subject_id": target_stack_id,
                        "item_instance_id": None,
                        "stack_id": target_stack_id,
                    }
                )
                self.function_runtime[target_key] = target_row
                if source_row.get("remaining_charges") is not None:
                    target_row["remaining_charges"] = _finite_number(
                        float(source_row["remaining_charges"]) * ratio,
                        field="function.remaining_charges",
                        minimum=0,
                    )
            else:
                left = target_row.get("remaining_charges")
                right = source_row.get("remaining_charges")
                if left is None and right is not None:
                    target_row["remaining_charges"] = _finite_number(
                        float(right) * ratio,
                        field="function.remaining_charges",
                        minimum=0,
                    )
                elif left is not None and right is not None:
                    target_row["remaining_charges"] = _finite_number(
                        float(left) + float(right) * ratio,
                        field="function.remaining_charges",
                        minimum=0,
                    )
                left_state = str(
                    target_row.get("unlock_state") or "unlocked"
                )
                right_state = str(
                    source_row.get("unlock_state") or "unlocked"
                )
                target_row["unlock_state"] = max(
                    (left_state, right_state),
                    key=lambda value: priority.get(value, 2),
                )
                target_row["enabled"] = int(
                    bool(target_row.get("enabled"))
                    and bool(source_row.get("enabled"))
                    and target_row["unlock_state"] == "unlocked"
                )
                target_row["cooldown_until_json"] = self._later_coordinate(
                    _coordinate(target_row.get("cooldown_until_json")),
                    _coordinate(source_row.get("cooldown_until_json")),
                )
            target_row.update(
                {
                    "source_event_id": source_event_id,
                    "story_coordinate_json": dict(coordinate),
                    "updated_order": int(updated_order),
                    "state_json": {
                        **self._state_payload(target_row),
                        "merged_from_stack_ids": sorted(
                            set(
                                self._state_payload(target_row).get(
                                    "merged_from_stack_ids", []
                                )
                            )
                            | {source_stack_id}
                        ),
                    },
                }
            )
            if source_row.get("remaining_charges") is not None:
                source_row["remaining_charges"] = _finite_number(
                    float(source_row["remaining_charges"]) * (1.0 - ratio),
                    field="function.remaining_charges",
                    minimum=0,
                )
            source_updates: dict[str, Any] = {
                "source_event_id": source_event_id,
                "story_coordinate_json": dict(coordinate),
                "updated_order": int(updated_order),
                "state_json": {
                    **self._state_payload(source_row),
                    "merged_quantity": moved_quantity,
                    "merged_into_stack_id": target_stack_id,
                },
            }
            if source_depleted:
                source_updates.update(
                    {
                        "enabled": 0,
                        "unlock_state": "suppressed",
                        "state_json": {
                            **source_updates["state_json"],
                            "suppressed_by": "stack_merged",
                        },
                    }
                )
            source_row.update(source_updates)

    def _cleanup_subject_runtime(
        self,
        subject_type: str,
        subject_id: str,
        *,
        reason: str,
        source_event_id: str,
        updated_order: int,
        coordinate: Mapping[str, Any],
        zero_charges: bool = False,
    ) -> None:
        for key, function_runtime in list(self.function_runtime.items()):
            if key[0] != subject_type or key[1] != subject_id:
                continue
            if zero_charges and function_runtime.get(
                "remaining_charges"
            ) is not None:
                function_runtime["remaining_charges"] = 0.0
            function_runtime.update(
                {
                    "enabled": 0,
                    "unlock_state": "suppressed",
                    "source_event_id": source_event_id,
                    "story_coordinate_json": dict(coordinate),
                    "updated_order": int(updated_order),
                    "state_json": {
                        **self._state_payload(function_runtime),
                        "suppressed_by": reason,
                    },
                }
            )
        target_field = (
            "item_instance_id"
            if subject_type == "item_instance"
            else "stack_id"
        )
        for binding in self.bindings.values():
            if binding.get(target_field) == subject_id:
                binding["binding_status"] = "deprecated"
                binding["source_event_id"] = source_event_id
                binding["updated_order"] = int(updated_order)

    def _apply_item_instance(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str,
        updated_order: int,
    ) -> None:
        action = str(event.get("action") or "instantiate")
        coordinate = self._require_story_coordinate(event)
        if action in {"split", "merge"}:
            source_id = str(event.get("source_stack_id") or "")
            target_id = str(event.get("target_stack_id") or "")
            quantity = _finite_number(
                event.get("quantity"),
                field="item_stack.quantity",
                minimum=0,
            )
            if quantity <= 0:
                raise ContinuityError(
                    "ITEM_INVALID_QUANTITY",
                    "stack split/merge quantity must be greater than zero",
                )
            source = self.stacks.get(source_id)
            if source is None or str(source.get("stack_status")) != "active":
                raise ContinuityError(
                    "ITEM_STACK_NOT_FOUND",
                    f"unknown active source stack: {source_id}",
                )
            source_quantity = float(source["quantity"])
            if source_quantity + _EPSILON < quantity:
                raise ContinuityError(
                    "ITEM_INSUFFICIENT_QUANTITY",
                    "stack split/merge exceeds the source quantity",
                    details={
                        "stack_id": source_id,
                        "available": source_quantity,
                        "requested": quantity,
                    },
                )
            source_definition_id = str(source["item_definition_id"])
            source_batch = self._batch_payload(source)
            if action == "split":
                if target_id in self.stacks:
                    raise ContinuityError(
                        "ITEM_STACK_ALREADY_EXISTS",
                        "split target stack already exists",
                        details={"stack_id": target_id},
                    )
                target_batch = dict(
                    event.get("target_batch")
                    if event.get("target_batch") is not None
                    else source_batch
                )
                self.stacks[target_id] = {
                    "stack_id": target_id,
                    "item_definition_id": source_definition_id,
                    "quantity": quantity,
                    "stack_status": "active",
                    "batch_json": target_batch,
                    "source_event_id": source_event_id,
                    "story_coordinate_json": coordinate,
                    "updated_order": int(updated_order),
                }
                source_custody = self.custody.get(
                    ("item_stack", source_id)
                )
                if source_custody is not None:
                    target_custody = deepcopy(source_custody)
                    target_custody.update(
                        {
                            "custody_key": stable_item_id(
                                "item_custody_",
                                {
                                    "subject_type": "item_stack",
                                    "subject_id": target_id,
                                },
                            ),
                            "subject_id": target_id,
                            "item_instance_id": None,
                            "stack_id": target_id,
                            "quantity": quantity,
                            "source_event_id": source_event_id,
                            "story_coordinate_json": coordinate,
                            "updated_order": int(updated_order),
                        }
                    )
                    self.custody[("item_stack", target_id)] = target_custody
                self._split_stack_function_runtime(
                    source_id,
                    target_id,
                    split_quantity=quantity,
                    source_quantity_before=source_quantity,
                    coordinate=coordinate,
                    source_event_id=source_event_id,
                    updated_order=updated_order,
                )
            else:
                target = self.stacks.get(target_id)
                if (
                    target is None
                    or str(target.get("stack_status")) != "active"
                ):
                    raise ContinuityError(
                        "ITEM_STACK_NOT_FOUND",
                        f"unknown active target stack: {target_id}",
                    )
                if str(target["item_definition_id"]) != source_definition_id:
                    raise ContinuityError(
                        "ITEM_STACK_DEFINITION_MISMATCH",
                        "merged stacks must use the same item definition",
                    )
                if self._batch_payload(target) != source_batch:
                    raise ContinuityError(
                        "ITEM_STACK_BATCH_MISMATCH",
                        "heterogeneous lots must remain separate stacks",
                    )
                source_custody = self.custody.get(
                    ("item_stack", source_id)
                )
                target_custody = self.custody.get(
                    ("item_stack", target_id)
                )
                if (
                    source_custody is not None
                    and target_custody is not None
                    and self._custody_anchor(source_custody)
                    != self._custody_anchor(target_custody)
                ):
                    raise ContinuityError(
                        "ITEM_STACK_CUSTODY_MISMATCH",
                        "stacks at different custody anchors cannot merge",
                    )
                target["quantity"] = _finite_number(
                    float(target["quantity"]) + quantity,
                    field="item_stack.quantity",
                    minimum=0,
                )
                target["source_event_id"] = source_event_id
                target["story_coordinate_json"] = coordinate
                target["updated_order"] = int(updated_order)
                if target_custody is not None:
                    target_custody["quantity"] = float(target["quantity"])
                    target_custody["source_event_id"] = source_event_id
                    target_custody["updated_order"] = int(updated_order)
                self._merge_stack_function_runtime(
                    source_id,
                    target_id,
                    moved_quantity=quantity,
                    source_quantity_before=source_quantity,
                    coordinate=coordinate,
                    source_event_id=source_event_id,
                    updated_order=updated_order,
                )
            source["quantity"] = _finite_number(
                source_quantity - quantity,
                field="item_stack.quantity",
                minimum=0,
            )
            source["source_event_id"] = source_event_id
            source["story_coordinate_json"] = coordinate
            source["updated_order"] = int(updated_order)
            source_custody = self.custody.get(("item_stack", source_id))
            if source["quantity"] <= _EPSILON:
                source["quantity"] = 0.0
                source["stack_status"] = (
                    "merged" if action == "merge" else "depleted"
                )
                self.custody.pop(("item_stack", source_id), None)
                self._cleanup_subject_runtime(
                    "item_stack",
                    source_id,
                    reason=(
                        "stack_merged" if action == "merge" else "stack_depleted"
                    ),
                    source_event_id=source_event_id,
                    updated_order=updated_order,
                    coordinate=coordinate,
                    zero_charges=True,
                )
            elif source_custody is not None:
                source_custody["quantity"] = float(source["quantity"])
                source_custody["source_event_id"] = source_event_id
                source_custody["updated_order"] = int(updated_order)
            self._validate_container_graph()
            return

        subject_type, subject_id = _subject(event)
        if action == "retire":
            if subject_type == "item_instance":
                row = self.instances.get(subject_id)
                if row is None:
                    raise ContinuityError(
                        "ITEM_INSTANCE_NOT_FOUND",
                        f"unknown item instance: {subject_id}",
                    )
                row["instance_status"] = "retired"
                runtime = self.runtime.get(subject_id)
                if runtime is not None:
                    runtime.update(
                        {
                            "active": 0,
                            "equipped_by_entity_id": None,
                            "slot_key": None,
                            "bound_actor_entity_id": None,
                            "source_event_id": source_event_id,
                            "story_coordinate_json": coordinate,
                            "updated_order": int(updated_order),
                        }
                    )
                self.custody.pop((subject_type, subject_id), None)
                self._cleanup_subject_runtime(
                    "item_instance",
                    subject_id,
                    reason="item_retired",
                    source_event_id=source_event_id,
                    updated_order=updated_order,
                    coordinate=coordinate,
                )
            else:
                row = self.stacks.get(subject_id)
                if row is None:
                    raise ContinuityError(
                        "ITEM_STACK_NOT_FOUND",
                        f"unknown item stack: {subject_id}",
                    )
                row["stack_status"] = "retired"
                self.custody.pop((subject_type, subject_id), None)
                self._cleanup_subject_runtime(
                    "item_stack",
                    subject_id,
                    reason="stack_retired",
                    source_event_id=source_event_id,
                    updated_order=updated_order,
                    coordinate=coordinate,
                )
            row["source_event_id"] = source_event_id
            row["story_coordinate_json"] = coordinate
            row["updated_order"] = int(updated_order)
            return

        definition_id = str(event.get("item_definition_id") or "")
        definition = self.definitions.get(definition_id)
        if (
            definition is None
            or str(definition.get("item_status")) != "active"
        ):
            raise ContinuityError(
                "ITEM_DEFINITION_NOT_FOUND",
                "item instantiation requires an active definition",
                details={"item_definition_id": definition_id},
            )
        stack_policy = str(definition.get("stack_policy"))
        uniqueness = str(definition.get("uniqueness_policy"))
        if subject_type == "item_stack":
            if stack_policy == "non_stackable" or uniqueness in {
                "unique_instance",
                "unique_definition",
            }:
                raise ContinuityError(
                    "ITEM_STACK_POLICY_CONFLICT",
                    "this item definition cannot be instantiated as a stack",
                    details={
                        "item_definition_id": definition_id,
                        "stack_policy": stack_policy,
                        "uniqueness_policy": uniqueness,
                    },
                )
            if subject_id in self.stacks:
                raise ContinuityError(
                    "ITEM_STACK_ALREADY_EXISTS",
                    f"item stack already exists: {subject_id}",
                )
            quantity = _finite_number(
                event.get("quantity"),
                field="item_stack.quantity",
                minimum=0,
            )
            if quantity <= 0:
                raise ContinuityError(
                    "ITEM_INVALID_QUANTITY",
                    "item stack quantity must be greater than zero",
                )
            self.stacks[subject_id] = {
                "stack_id": subject_id,
                "item_definition_id": definition_id,
                "quantity": quantity,
                "stack_status": "active",
                "batch_json": dict(event.get("batch") or {}),
                "source_event_id": source_event_id,
                "story_coordinate_json": coordinate,
                "updated_order": int(updated_order),
            }
            for function_id in sorted(
                self._active_function_bindings(
                    "item_stack", subject_id, definition_id
                )
            ):
                if function_id in self.functions:
                    self._ensure_function_runtime(
                        "item_stack",
                        subject_id,
                        function_id,
                        source_event_id=source_event_id,
                        updated_order=updated_order,
                        coordinate=coordinate,
                    )
            return

        if subject_id in self.instances:
            raise ContinuityError(
                "ITEM_INSTANCE_ALREADY_EXISTS",
                f"item instance already exists: {subject_id}",
            )
        if uniqueness == "unique_definition":
            conflicts = sorted(
                instance_id
                for instance_id, instance in self.instances.items()
                if str(instance["item_definition_id"]) == definition_id
                and str(instance.get("instance_status")) in {
                    "active",
                    LEGACY_ITEM_SELF_INSTANCE,
                }
            )
            if conflicts:
                raise ContinuityError(
                    "ITEM_UNIQUE_DEFINITION_CONFLICT",
                    "a unique-definition item already has an active instance",
                    details={"item_instance_ids": conflicts},
                )
        attributes = dict(event.get("attributes") or {})
        item_entity_id = event.get("item_entity_id") or attributes.get(
            "item_entity_id"
        )
        if item_entity_id:
            entity_conflicts = sorted(
                instance_id
                for instance_id, instance in self.instances.items()
                if instance_id != subject_id
                and str(instance.get("item_entity_id") or "")
                == str(item_entity_id)
                and str(instance.get("instance_status"))
                in {"active", LEGACY_ITEM_SELF_INSTANCE}
            )
            if entity_conflicts:
                raise ContinuityError(
                    "ITEM_INSTANCE_ENTITY_UNIQUENESS_CONFLICT",
                    "item entity is already assigned to another active instance",
                    details={
                        "item_entity_id": str(item_entity_id),
                        "item_instance_ids": entity_conflicts,
                    },
                )
        serial_or_mark = event.get("serial_or_mark")
        if serial_or_mark is None:
            serial_or_mark = attributes.get("serial_or_mark")
        if serial_or_mark is not None:
            serial_text = str(serial_or_mark).strip()
            if not serial_text:
                raise ContinuityError(
                    "ITEM_INVALID_IDENTIFIER",
                    "serial_or_mark cannot be empty when supplied",
                )
            serial_conflicts = sorted(
                instance_id
                for instance_id, instance in self.instances.items()
                if instance_id != subject_id
                and str(instance.get("item_definition_id")) == definition_id
                and str(
                    self._instance_payload(instance).get("serial_or_mark")
                    or ""
                )
                == serial_text
                and str(instance.get("instance_status"))
                in {"active", LEGACY_ITEM_SELF_INSTANCE}
            )
            if serial_conflicts:
                raise ContinuityError(
                    "ITEM_SERIAL_UNIQUENESS_CONFLICT",
                    "serial_or_mark must be unique within an item definition",
                    details={
                        "serial_or_mark": serial_text,
                        "item_instance_ids": serial_conflicts,
                    },
                )
            serial_or_mark = serial_text
        unique = event.get("unique", attributes.get("unique"))
        if unique is None:
            unique = (
                True
                if uniqueness in {"unique_instance", "unique_definition"}
                else "unknown"
            )
        if unique != "unknown" and not isinstance(unique, bool):
            raise ContinuityError(
                "ITEM_INVALID_ENUM",
                "item instance unique must be boolean or 'unknown'",
                details={"field": "unique", "value": unique},
            )
        if (
            uniqueness in {"unique_instance", "unique_definition"}
            and unique is not True
        ):
            raise ContinuityError(
                "ITEM_UNIQUENESS_VIOLATION",
                "the item definition requires explicitly unique instances",
                details={
                    "item_definition_id": definition_id,
                    "uniqueness_policy": uniqueness,
                    "unique": unique,
                },
            )
        instance_name = event.get("instance_name")
        if instance_name is None:
            instance_name = attributes.get("instance_name")
        provenance = event.get("provenance")
        if provenance is None:
            provenance = attributes.get("provenance")
        self.instances[subject_id] = {
            "item_instance_id": subject_id,
            "item_definition_id": definition_id,
            "item_entity_id": (
                str(item_entity_id) if item_entity_id else None
            ),
            "instance_status": "active",
            "instance_json": {
                "item_instance_id": subject_id,
                "item_definition_id": definition_id,
                "instance_name": instance_name,
                "serial_or_mark": serial_or_mark,
                "unique": unique,
                "provenance": provenance,
                "attributes": attributes,
            },
            "source_event_id": source_event_id,
            "story_coordinate_json": coordinate,
            "updated_order": int(updated_order),
        }
        self._ensure_instance_runtime(
            subject_id,
            source_event_id=source_event_id,
            updated_order=updated_order,
            coordinate=coordinate,
        )

    @staticmethod
    def _custody_anchor(
        custody: Mapping[str, Any],
    ) -> tuple[Any, ...]:
        return (
            custody.get("custodian_entity_id"),
            custody.get("carrier_entity_id"),
            custody.get("container_instance_id"),
            custody.get("location_entity_id"),
            custody.get("access_controller_entity_id"),
            custody.get("legal_owner_entity_id"),
        )

    def _resolved_location(
        self,
        subject_type: str,
        subject_id: str,
        *,
        seen: set[str] | None = None,
    ) -> str | None:
        custody = self.custody.get((subject_type, subject_id))
        if custody is None:
            return None
        if custody.get("location_entity_id") is not None:
            return str(custody["location_entity_id"])
        container_id = custody.get("container_instance_id")
        if not container_id:
            carrier = custody.get("carrier_entity_id")
            if carrier:
                return self.locations.get(str(carrier))
            return None
        visited = set(seen or ())
        container = str(container_id)
        if container in visited:
            raise ContinuityError(
                "ITEM_CONTAINER_CYCLE",
                "item container chain contains a cycle",
                details={"item_instance_id": container},
            )
        visited.add(container)
        return self._resolved_location(
            "item_instance",
            container,
            seen=visited,
        )

    def _subject_bulk(
        self,
        subject_type: str,
        subject_id: str,
    ) -> float:
        definition_id, definition = self._definition_for_subject(
            subject_type, subject_id
        )
        payload = self._definition_payload(definition)
        unit_bulk = _finite_number(
            payload.get("unit_bulk", 1),
            field=f"{definition_id}.unit_bulk",
            minimum=0,
        )
        quantity = (
            1.0
            if subject_type == "item_instance"
            else float(self.stacks[subject_id]["quantity"])
        )
        return unit_bulk * quantity

    def _validate_container_graph(self) -> None:
        graph: dict[str, str] = {}
        for (subject_type, subject_id), custody in self.custody.items():
            container_id = custody.get("container_instance_id")
            if not container_id:
                continue
            container = str(container_id)
            if subject_type == "item_instance" and subject_id == container:
                raise ContinuityError(
                    "ITEM_CONTAINER_CYCLE",
                    "an item instance cannot contain itself",
                    details={"item_instance_id": subject_id},
                )
            container_instance = self.instances.get(container)
            if (
                container_instance is None
                or str(container_instance.get("instance_status")) != "active"
            ):
                raise ContinuityError(
                    "ITEM_CONTAINER_NOT_FOUND",
                    "item custody references a missing active container",
                    details={"container_instance_id": container},
                )
            container_definition = self.definitions.get(
                str(container_instance["item_definition_id"])
            )
            if container_definition is None:
                raise ContinuityError(
                    "ITEM_CONTAINER_NOT_FOUND",
                    "container instance has no accepted definition",
                    details={"container_instance_id": container},
                )
            kind = str(container_definition.get("item_kind") or "")
            if kind not in {"container", "body_or_vessel", "transport"}:
                raise ContinuityError(
                    "ITEM_CONTAINER_KIND_INVALID",
                    "only a container-capable item may hold another item",
                    details={
                        "container_instance_id": container,
                        "item_kind": kind,
                    },
                )
            if subject_type == "item_instance":
                graph[subject_id] = container

        for start in sorted(graph):
            path: list[str] = []
            cursor = start
            while cursor in graph:
                if cursor in path:
                    cycle_start = path.index(cursor)
                    raise ContinuityError(
                        "ITEM_CONTAINER_CYCLE",
                        "item container chain contains a cycle",
                        details={"item_instance_ids": path[cycle_start:]},
                    )
                path.append(cursor)
                cursor = graph[cursor]

        used_capacity: dict[str, float] = {}
        for (subject_type, subject_id), custody in self.custody.items():
            container_id = custody.get("container_instance_id")
            if not container_id:
                continue
            container = str(container_id)
            used_capacity[container] = used_capacity.get(
                container, 0.0
            ) + self._subject_bulk(subject_type, subject_id)
        for container, used in sorted(used_capacity.items()):
            instance = self.instances[container]
            definition = self._definition_payload(
                self.definitions[str(instance["item_definition_id"])]
            )
            capacity = definition.get("capacity")
            if capacity is None:
                continue
            maximum = _finite_number(
                capacity,
                field=f"{container}.capacity",
                minimum=0,
            )
            if used > maximum + _EPSILON:
                raise ContinuityError(
                    "ITEM_CONTAINER_CAPACITY_EXCEEDED",
                    "container contents exceed accepted capacity",
                    details={
                        "container_instance_id": container,
                        "capacity": maximum,
                        "used": used,
                    },
                )

    def _apply_item_custody(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str,
        updated_order: int,
    ) -> None:
        coordinate = self._require_story_coordinate(event)
        subject_type, subject_id = _subject(event)
        self._definition_for_subject(subject_type, subject_id)
        action = str(event.get("action") or "acquire")
        requested_status = (
            _require_enum(
                event.get("custody_status"),
                field="custody_status",
                choices=ITEM_CUSTODY_STATUSES,
            )
            if event.get("custody_status") is not None
            else None
        )
        key = (subject_type, subject_id)
        current = deepcopy(
            self.custody.get(
                key,
                {
                    "custody_key": stable_item_id(
                        "item_custody_",
                        {
                            "subject_type": subject_type,
                            "subject_id": subject_id,
                        },
                    ),
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "item_instance_id": (
                        subject_id
                        if subject_type == "item_instance"
                        else None
                    ),
                    "stack_id": (
                        subject_id if subject_type == "item_stack" else None
                    ),
                    "legal_owner_entity_id": None,
                    "custodian_entity_id": None,
                    "carrier_entity_id": None,
                    "location_entity_id": None,
                    "container_instance_id": None,
                    "access_controller_entity_id": None,
                    "custody_status": "lost",
                    "quantity": (
                        1.0
                        if subject_type == "item_instance"
                        else float(self.stacks[subject_id]["quantity"])
                    ),
                    "state_json": {},
                },
            )
        )

        checks = {
            "from_legal_owner_entity_id": "legal_owner_entity_id",
            "from_custodian_entity_id": "custodian_entity_id",
            "from_carrier_entity_id": "carrier_entity_id",
            "from_location_entity_id": "location_entity_id",
            "from_container_instance_id": "container_instance_id",
            "from_access_controller_entity_id": (
                "access_controller_entity_id"
            ),
        }
        for event_field, state_field in checks.items():
            expected = event.get(event_field)
            if expected is None:
                continue
            actual = current.get(state_field)
            if str(expected) != str(actual):
                raise ContinuityError(
                    "ITEM_CUSTODY_ORIGIN_MISMATCH",
                    "item custody change does not start at accepted state",
                    details={
                        "field": event_field,
                        "expected": actual,
                        "actual": expected,
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                    },
                )

        if action == "transfer_title":
            current_owner = current.get("legal_owner_entity_id")
            from_owner = event.get("from_legal_owner_entity_id")
            if (
                current_owner is not None
                and from_owner is not None
                and str(current_owner) != str(from_owner)
            ):
                raise ContinuityError(
                    "ITEM_TITLE_OWNER_MISMATCH",
                    "title transfer names a non-owner",
                )
            current["legal_owner_entity_id"] = str(
                event["to_legal_owner_entity_id"]
            )
        elif action in {"lose", "abandon"}:
            current.update(
                {
                    "custodian_entity_id": None,
                    "carrier_entity_id": None,
                    "location_entity_id": None,
                    "container_instance_id": None,
                    "access_controller_entity_id": None,
                    "custody_status": (
                        "lost" if action == "lose" else "abandoned"
                    ),
                }
            )
        else:
            if action == "acquire" and event.get(
                "to_legal_owner_entity_id"
            ) is not None:
                current["legal_owner_entity_id"] = str(
                    event["to_legal_owner_entity_id"]
                )
            destinations = {
                "custodian_entity_id": event.get(
                    "to_custodian_entity_id"
                ),
                "carrier_entity_id": event.get("to_carrier_entity_id"),
                "location_entity_id": event.get("to_location_entity_id"),
                "container_instance_id": event.get(
                    "to_container_instance_id"
                ),
                "access_controller_entity_id": event.get(
                    "to_access_controller_entity_id"
                ),
            }
            explicit_physical = any(
                event.get(field) is not None
                for field in (
                    "to_carrier_entity_id",
                    "to_container_instance_id",
                    "to_location_entity_id",
                )
            )
            if explicit_physical:
                current["carrier_entity_id"] = None
                current["container_instance_id"] = None
                current["location_entity_id"] = None
            for field, value in destinations.items():
                if value is not None:
                    current[field] = str(value)
            if (
                current.get("carrier_entity_id")
                and current.get("container_instance_id")
            ):
                raise ContinuityError(
                    "ITEM_CUSTODY_ANCHOR_AMBIGUOUS",
                    "item cannot be directly carried and directly contained at once",
                    details={
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                    },
                )
            status_by_action = {
                "loan": "loaned",
                "seize": "seized",
                "store": "stored",
                "handover": "possessed",
                "return": "possessed",
                "retrieve": "possessed",
                "recover": "possessed",
                "acquire": "possessed",
            }
            current["custody_status"] = status_by_action.get(
                action, "possessed"
            )
        if requested_status is not None:
            current["custody_status"] = requested_status
            if requested_status in {"lost", "abandoned", "destroyed"}:
                current["carrier_entity_id"] = None
                current["container_instance_id"] = None
                current["location_entity_id"] = None
                current["access_controller_entity_id"] = None
            if requested_status == "destroyed":
                current["custodian_entity_id"] = None

        expected_quantity = (
            1.0
            if subject_type == "item_instance"
            else float(self.stacks[subject_id]["quantity"])
        )
        if event.get("quantity") is not None and not _same_number(
            event["quantity"], expected_quantity
        ):
            raise ContinuityError(
                "ITEM_PARTIAL_STACK_CUSTODY_FORBIDDEN",
                "partial stack custody requires an explicit split first",
                details={
                    "stack_id": (
                        subject_id if subject_type == "item_stack" else None
                    ),
                    "stack_quantity": expected_quantity,
                    "event_quantity": event.get("quantity"),
                },
            )
        current.update(
            {
                "quantity": expected_quantity,
                "source_event_id": source_event_id,
                "story_coordinate_json": coordinate,
                "updated_order": int(updated_order),
                "state_json": {
                    **self._state_payload(current),
                    "last_action": action,
                },
            }
        )
        self.custody[key] = current
        self._validate_container_graph()

    def _actor_can_access(
        self,
        actor_entity_id: str,
        subject_type: str,
        subject_id: str,
    ) -> bool:
        custody = self.custody.get((subject_type, subject_id))
        if custody is None:
            return False
        direct = {
            str(value)
            for value in (
                custody.get("legal_owner_entity_id"),
                custody.get("custodian_entity_id"),
                custody.get("carrier_entity_id"),
                custody.get("access_controller_entity_id"),
            )
            if value is not None
        }
        if actor_entity_id in direct:
            return True
        container_id = custody.get("container_instance_id")
        if container_id and self._actor_can_access(
            actor_entity_id,
            "item_instance",
            str(container_id),
        ):
            return True
        if subject_type == "item_instance":
            runtime = self.runtime.get(subject_id)
            if runtime is not None and actor_entity_id in {
                str(value)
                for value in (
                    runtime.get("equipped_by_entity_id"),
                    runtime.get("bound_actor_entity_id"),
                )
                if value is not None
            }:
                return True
        return False

    def _validate_actor_location(
        self,
        actor_entity_id: str,
        subject_type: str,
        subject_id: str,
    ) -> None:
        item_location = self._resolved_location(subject_type, subject_id)
        actor_location = self.locations.get(actor_entity_id)
        if (
            item_location is not None
            and actor_location is not None
            and str(item_location) != str(actor_location)
        ):
            raise ContinuityError(
                "ITEM_LOCATION_MISMATCH",
                "actor and item are at incompatible accepted locations",
                details={
                    "actor_entity_id": actor_entity_id,
                    "actor_location_entity_id": actor_location,
                    "item_location_entity_id": item_location,
                },
            )

    def _validate_slot_available(
        self,
        actor_entity_id: str,
        slot_key: str,
        item_instance_id: str,
    ) -> None:
        conflicts = sorted(
            instance_id
            for instance_id, runtime in self.runtime.items()
            if instance_id != item_instance_id
            and not bool(runtime.get("destroyed"))
            and runtime.get("equipped_by_entity_id") == actor_entity_id
            and runtime.get("slot_key") == slot_key
        )
        if conflicts:
            raise ContinuityError(
                "ITEM_EQUIPMENT_SLOT_OCCUPIED",
                "another active item already occupies this equipment slot",
                details={
                    "actor_entity_id": actor_entity_id,
                    "slot_key": slot_key,
                    "item_instance_ids": conflicts,
                },
            )

    def _destroy_instance(
        self,
        item_instance_id: str,
        *,
        source_event_id: str,
        updated_order: int,
        coordinate: Mapping[str, Any],
        consumed: bool = False,
    ) -> None:
        instance = self.instances[item_instance_id]
        instance["instance_status"] = "consumed" if consumed else "destroyed"
        instance["source_event_id"] = source_event_id
        instance["story_coordinate_json"] = dict(coordinate)
        instance["updated_order"] = int(updated_order)
        runtime = self._ensure_instance_runtime(
            item_instance_id,
            source_event_id=source_event_id,
            updated_order=updated_order,
            coordinate=coordinate,
        )
        runtime.update(
            {
                "durability": 0.0
                if runtime.get("durability") is not None
                else None,
                "damaged": 1,
                "destroyed": 1,
                "active": 0,
                "equipped_by_entity_id": None,
                "slot_key": None,
                "bound_actor_entity_id": None,
                "source_event_id": source_event_id,
                "story_coordinate_json": dict(coordinate),
                "updated_order": int(updated_order),
            }
        )
        self.custody.pop(("item_instance", item_instance_id), None)
        self._cleanup_subject_runtime(
            "item_instance",
            item_instance_id,
            reason="item_consumed" if consumed else "item_destroyed",
            source_event_id=source_event_id,
            updated_order=updated_order,
            coordinate=coordinate,
            zero_charges=consumed,
        )
        self._validate_container_graph()

    def _apply_item_runtime(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str,
        updated_order: int,
    ) -> None:
        coordinate = self._require_story_coordinate(event)
        subject_type, item_instance_id = _subject(event)
        if subject_type != "item_instance":
            raise ContinuityError(
                "ITEM_RUNTIME_INSTANCE_REQUIRED",
                "item runtime changes require an item instance",
            )
        self._definition_for_subject(subject_type, item_instance_id)
        runtime = self._ensure_instance_runtime(
            item_instance_id,
            source_event_id=source_event_id,
            updated_order=updated_order,
            coordinate=coordinate,
        )
        action = str(event.get("action") or "")
        delta = dict(event.get("delta") or {})
        if bool(runtime.get("destroyed")) and action != "destroy":
            raise ContinuityError(
                "ITEM_DESTROYED",
                "destroyed item instances cannot change runtime state",
                details={"item_instance_id": item_instance_id},
            )

        if action == "bootstrap":
            numeric_fields = (
                "durability",
                "max_durability",
                "energy",
                "max_energy",
            )
            for field in numeric_fields:
                if field not in event:
                    continue
                value = event.get(field)
                runtime[field] = (
                    _finite_number(
                        value,
                        field=f"item_runtime.{field}",
                        minimum=0,
                    )
                    if value is not None
                    else None
                )
            for field in ("sealed", "damaged", "destroyed", "active"):
                if field in event:
                    runtime[field] = int(
                        _require_boolean(
                            event[field],
                            field=f"item_runtime.{field}",
                        )
                    )
            if (
                runtime.get("durability") is not None
                and runtime.get("max_durability") is not None
                and float(runtime["durability"])
                > float(runtime["max_durability"]) + _EPSILON
            ):
                raise ContinuityError(
                    "ITEM_DURABILITY_CAPACITY_EXCEEDED",
                    "bootstrap durability exceeds max_durability",
                    details={
                        "durability": runtime["durability"],
                        "max_durability": runtime["max_durability"],
                    },
                )
            if (
                runtime.get("energy") is not None
                and runtime.get("max_energy") is not None
                and float(runtime["energy"])
                > float(runtime["max_energy"]) + _EPSILON
            ):
                raise ContinuityError(
                    "ITEM_ENERGY_CAPACITY_EXCEEDED",
                    "bootstrap energy exceeds max_energy",
                    details={
                        "energy": runtime["energy"],
                        "max_energy": runtime["max_energy"],
                    },
                )
            equipped_actor = event.get("equipped_by_entity_id")
            slot_key_value = event.get("slot_key")
            if (equipped_actor is None) != (slot_key_value is None):
                raise ContinuityError(
                    "ITEM_EQUIPMENT_PAIR_REQUIRED",
                    "equipped_by_entity_id and slot_key must be supplied together",
                )
            if equipped_actor is not None:
                actor = str(equipped_actor).strip()
                slot_key = str(slot_key_value or "").strip()
                if not actor or not slot_key:
                    raise ContinuityError(
                        "ITEM_EQUIPMENT_PAIR_REQUIRED",
                        "bootstrap equipment actor and slot must be non-empty",
                    )
                if not self._actor_can_access(
                    actor, "item_instance", item_instance_id
                ):
                    raise ContinuityError(
                        "ITEM_ACCESS_DENIED",
                        "bootstrap equipment actor has no accepted item access",
                    )
                self._validate_slot_available(
                    actor, slot_key, item_instance_id
                )
                runtime["equipped_by_entity_id"] = actor
                runtime["slot_key"] = slot_key
            if "bound_actor_entity_id" in event:
                bound_actor = event.get("bound_actor_entity_id")
                if bound_actor is None:
                    runtime["bound_actor_entity_id"] = None
                else:
                    actor = str(bound_actor).strip()
                    if not actor:
                        raise ContinuityError(
                            "ITEM_INVALID_IDENTIFIER",
                            "bound_actor_entity_id cannot be empty",
                        )
                    if not self._actor_can_access(
                        actor, "item_instance", item_instance_id
                    ):
                        raise ContinuityError(
                            "ITEM_ACCESS_DENIED",
                            "bootstrap bound actor has no accepted item access",
                        )
                    runtime["bound_actor_entity_id"] = actor
            state = event.get("state")
            if state is not None:
                if not isinstance(state, Mapping):
                    raise ContinuityError(
                        "ITEM_INVALID_STATE",
                        "item runtime bootstrap state must be an object",
                    )
                runtime["state_json"] = {
                    **self._state_payload(runtime),
                    **dict(state),
                }
            if bool(runtime.get("destroyed")):
                self._destroy_instance(
                    item_instance_id,
                    source_event_id=source_event_id,
                    updated_order=updated_order,
                    coordinate=coordinate,
                )
                return
            if bool(runtime.get("sealed")) and bool(runtime.get("active")):
                raise ContinuityError(
                    "ITEM_ACTIVATION_CONFLICT",
                    "a sealed item cannot bootstrap as active",
                )
            if (
                runtime.get("durability") is not None
                and float(runtime["durability"]) <= _EPSILON
                and bool(runtime.get("active"))
            ):
                raise ContinuityError(
                    "ITEM_ACTIVATION_CONFLICT",
                    "an item with zero durability cannot bootstrap as active",
                )
        elif action == "equip":
            actor = str(event.get("actor_entity_id") or "")
            slot_key = str(event.get("slot_key") or "").strip()
            if not actor or not slot_key:
                raise ContinuityError(
                    "ITEM_EQUIPMENT_PAIR_REQUIRED",
                    "equip requires a non-empty actor and slot_key",
                )
            if not self._actor_can_access(
                actor, "item_instance", item_instance_id
            ):
                raise ContinuityError(
                    "ITEM_ACCESS_DENIED",
                    "actor has no accepted custody or access to equip the item",
                    details={
                        "actor_entity_id": actor,
                        "item_instance_id": item_instance_id,
                    },
                )
            self._validate_actor_location(
                actor, "item_instance", item_instance_id
            )
            self._validate_slot_available(actor, slot_key, item_instance_id)
            runtime["equipped_by_entity_id"] = actor
            runtime["slot_key"] = slot_key
        elif action == "unequip":
            actor = event.get("actor_entity_id")
            equipped = runtime.get("equipped_by_entity_id")
            if actor is not None and equipped is not None and str(actor) != str(
                equipped
            ):
                raise ContinuityError(
                    "ITEM_EQUIPPED_ACTOR_MISMATCH",
                    "unequip actor does not match accepted equipment state",
                )
            runtime["equipped_by_entity_id"] = None
            runtime["slot_key"] = None
        elif action == "bind":
            actor = str(event.get("actor_entity_id") or "")
            if not self._actor_can_access(
                actor, "item_instance", item_instance_id
            ):
                raise ContinuityError(
                    "ITEM_ACCESS_DENIED",
                    "actor has no accepted custody or access to bind the item",
                )
            current_actor = runtime.get("bound_actor_entity_id")
            if current_actor is not None and str(current_actor) != actor:
                raise ContinuityError(
                    "ITEM_BINDING_CONFLICT",
                    "item is already bound to another actor",
                    details={"bound_actor_entity_id": current_actor},
                )
            runtime["bound_actor_entity_id"] = actor
        elif action == "unbind":
            actor = event.get("actor_entity_id")
            bound = runtime.get("bound_actor_entity_id")
            if actor is not None and bound is not None and str(actor) != str(
                bound
            ):
                raise ContinuityError(
                    "ITEM_BOUND_ACTOR_MISMATCH",
                    "unbind actor does not match accepted binding state",
                )
            runtime["bound_actor_entity_id"] = None
        elif action == "activate":
            if bool(runtime.get("sealed")):
                raise ContinuityError(
                    "ITEM_SEALED",
                    "sealed item cannot be activated",
                )
            if (
                runtime.get("durability") is not None
                and float(runtime["durability"]) <= _EPSILON
            ):
                raise ContinuityError(
                    "ITEM_BROKEN",
                    "broken item cannot be activated",
                )
            runtime["active"] = 1
        elif action == "deactivate":
            runtime["active"] = 0
        elif action in {"charge", "discharge"}:
            if runtime.get("energy") is None:
                raise ContinuityError(
                    "ITEM_ENERGY_UNMODELED",
                    "item definition has no modeled energy capacity",
                )
            amount = _finite_number(
                delta.get("energy"),
                field="delta.energy",
                minimum=0,
            )
            current = float(runtime["energy"])
            maximum = runtime.get("max_energy")
            after = current + amount if action == "charge" else current - amount
            if after < -_EPSILON:
                raise ContinuityError(
                    "ITEM_INSUFFICIENT_ENERGY",
                    "item energy cannot be discharged below zero",
                    details={"available": current, "requested": amount},
                )
            if maximum is not None and after > float(maximum) + _EPSILON:
                raise ContinuityError(
                    "ITEM_ENERGY_CAPACITY_EXCEEDED",
                    "item charge exceeds max_energy",
                    details={"max_energy": maximum, "result": after},
                )
            runtime["energy"] = _finite_number(
                after, field="item.energy", minimum=0
            )
        elif action in {"repair", "damage"}:
            if runtime.get("durability") is None:
                raise ContinuityError(
                    "ITEM_DURABILITY_UNMODELED",
                    "item definition has no modeled durability",
                )
            amount = _finite_number(
                delta.get("durability"),
                field="delta.durability",
                minimum=0,
            )
            current = float(runtime["durability"])
            maximum = runtime.get("max_durability")
            after = current + amount if action == "repair" else current - amount
            if after < -_EPSILON:
                raise ContinuityError(
                    "ITEM_INSUFFICIENT_DURABILITY",
                    "item damage exceeds remaining durability",
                    details={"available": current, "requested": amount},
                )
            if maximum is not None and after > float(maximum) + _EPSILON:
                raise ContinuityError(
                    "ITEM_DURABILITY_CAPACITY_EXCEEDED",
                    "item repair exceeds max_durability",
                    details={"max_durability": maximum, "result": after},
                )
            runtime["durability"] = _finite_number(
                after, field="item.durability", minimum=0
            )
            runtime["damaged"] = int(
                maximum is not None
                and float(runtime["durability"]) < float(maximum) - _EPSILON
            )
            if float(runtime["durability"]) <= _EPSILON:
                runtime["active"] = 0
        elif action == "break":
            if runtime.get("durability") is None:
                raise ContinuityError(
                    "ITEM_DURABILITY_UNMODELED",
                    "item definition has no modeled durability",
                )
            runtime.update(
                {
                    "durability": 0.0,
                    "damaged": 1,
                    "active": 0,
                    "equipped_by_entity_id": None,
                    "slot_key": None,
                }
            )
        elif action in {"consume", "destroy"}:
            self._destroy_instance(
                item_instance_id,
                source_event_id=source_event_id,
                updated_order=updated_order,
                coordinate=coordinate,
                consumed=action == "consume",
            )
            return
        elif action == "seal":
            runtime["sealed"] = 1
            runtime["active"] = 0
        elif action == "unseal":
            runtime["sealed"] = 0
        elif action in {"unlock_function", "suppress_function"}:
            function_id = str(event.get("function_id") or "")
            self._function_for_subject(
                "item_instance", item_instance_id, function_id
            )
            function_runtime = self._ensure_function_runtime(
                "item_instance",
                item_instance_id,
                function_id,
                source_event_id=source_event_id,
                updated_order=updated_order,
                coordinate=coordinate,
            )
            if action == "unlock_function":
                function_runtime["enabled"] = 1
                function_runtime["unlock_state"] = "unlocked"
                state_payload = self._state_payload(function_runtime)
                state_payload.pop("suppressed_by", None)
                function_runtime["state_json"] = state_payload
            else:
                function_runtime["enabled"] = 0
                function_runtime["unlock_state"] = "suppressed"
                function_runtime["state_json"] = {
                    **self._state_payload(function_runtime),
                    "suppressed_by": event.get("reason")
                    or "accepted_item_event",
                }
            function_runtime.update(
                {
                    "source_event_id": source_event_id,
                    "story_coordinate_json": coordinate,
                    "updated_order": int(updated_order),
                }
            )
        else:
            raise ContinuityError(
                "ITEM_RUNTIME_ACTION_UNSUPPORTED",
                f"unsupported item runtime action: {action}",
            )
        runtime.update(
            {
                "source_event_id": source_event_id,
                "story_coordinate_json": coordinate,
                "updated_order": int(updated_order),
                "state_json": {
                    **self._state_payload(runtime),
                    "last_action": action,
                },
            }
        )

    def _apply_item_function_runtime(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str,
        updated_order: int,
    ) -> None:
        coordinate = self._require_story_coordinate(event)
        subject_type, subject_id = _subject(event)
        definition_id, _ = self._definition_for_subject(
            subject_type, subject_id
        )
        function_id = str(event.get("function_id") or "").strip()
        if not function_id:
            raise ContinuityError(
                "ITEM_FUNCTION_NOT_FOUND",
                "item_function_runtime requires function_id",
            )
        action = _require_enum(
            event.get("action"),
            field="action",
            choices=ITEM_FUNCTION_RUNTIME_ACTIONS,
        )
        require_active_binding = action in {"enable", "unlock"}
        function = self._function_for_subject(
            subject_type,
            subject_id,
            function_id,
            require_active_binding=require_active_binding,
        )
        row = self._ensure_function_runtime(
            subject_type,
            subject_id,
            function_id,
            source_event_id=source_event_id,
            updated_order=updated_order,
            coordinate=coordinate,
        )
        function_definition = self._definition_payload(function)

        if action == "bootstrap":
            if "unlock_state" in event:
                row["unlock_state"] = _require_enum(
                    event["unlock_state"],
                    field="unlock_state",
                    choices=ITEM_FUNCTION_UNLOCK_STATES,
                )
            if "enabled" in event:
                enabled = _require_boolean(
                    event["enabled"], field="enabled"
                )
                if enabled and function_id not in self._active_function_bindings(
                    subject_type, subject_id, definition_id
                ):
                    raise ContinuityError(
                        "ITEM_FUNCTION_NOT_BOUND",
                        "enabled function runtime requires an active enabled binding",
                        details={
                            "subject_type": subject_type,
                            "subject_id": subject_id,
                            "function_id": function_id,
                        },
                    )
                row["enabled"] = int(enabled)
            if "remaining_charges" in event:
                remaining = event.get("remaining_charges")
                row["remaining_charges"] = (
                    _finite_number(
                        remaining,
                        field="remaining_charges",
                        minimum=0,
                    )
                    if remaining is not None
                    else None
                )
            if "cooldown_until" in event:
                raw_cooldown = event.get("cooldown_until")
                cooldown = (
                    _coordinate(raw_cooldown)
                    if raw_cooldown is not None
                    else None
                )
                if raw_cooldown is not None and cooldown is None:
                    raise ContinuityError(
                        "ITEM_INVALID_COOLDOWN",
                        "cooldown_until must be a story coordinate or null",
                    )
                if (
                    cooldown is not None
                    and str(cooldown["calendar_id"])
                    != str(coordinate["calendar_id"])
                ):
                    raise ContinuityError(
                        "ITEM_STORY_COORDINATE_CONFLICT",
                        "function runtime cooldown uses another story calendar",
                    )
                row["cooldown_until_json"] = cooldown
            state = event.get("state")
            if state is not None:
                if not isinstance(state, Mapping):
                    raise ContinuityError(
                        "ITEM_INVALID_STATE",
                        "function runtime bootstrap state must be an object",
                    )
                row["state_json"] = {
                    **self._state_payload(row),
                    **dict(state),
                }
        elif action == "enable":
            if str(row.get("unlock_state")) != "unlocked":
                raise ContinuityError(
                    "ITEM_FUNCTION_LOCKED",
                    "a locked or suppressed function cannot be enabled",
                    details={"unlock_state": row.get("unlock_state")},
                )
            row["enabled"] = 1
        elif action == "disable":
            row["enabled"] = 0
        elif action == "unlock":
            row["unlock_state"] = "unlocked"
            row["enabled"] = 1
            state = self._state_payload(row)
            state.pop("suppressed_by", None)
            row["state_json"] = state
        elif action == "lock":
            row["enabled"] = 0
            row["unlock_state"] = "locked"
        elif action == "suppress":
            row["enabled"] = 0
            row["unlock_state"] = "suppressed"
            row["state_json"] = {
                **self._state_payload(row),
                "suppressed_by": event.get("reason")
                or "accepted_item_function_runtime",
            }
        elif action == "set_charges":
            raw_remaining = event.get("remaining_charges")
            if raw_remaining is None and isinstance(
                event.get("delta"), Mapping
            ):
                raw_remaining = event["delta"].get("charges")
            if raw_remaining is None:
                raise ContinuityError(
                    "ITEM_CHARGES_REQUIRED",
                    "set_charges requires remaining_charges",
                )
            row["remaining_charges"] = _finite_number(
                raw_remaining,
                field="remaining_charges",
                minimum=0,
            )
        elif action == "set_cooldown":
            cooldown = _coordinate(event.get("cooldown_until"))
            if cooldown is None:
                raise ContinuityError(
                    "ITEM_INVALID_COOLDOWN",
                    "set_cooldown requires cooldown_until story coordinate",
                )
            if str(cooldown["calendar_id"]) != str(
                coordinate["calendar_id"]
            ):
                raise ContinuityError(
                    "ITEM_STORY_COORDINATE_CONFLICT",
                    "function runtime cooldown uses another story calendar",
                )
            row["cooldown_until_json"] = cooldown
        elif action == "clear_cooldown":
            row["cooldown_until_json"] = None

        default_charges = function_definition.get("charges")
        if (
            default_charges is not None
            and row.get("remaining_charges") is not None
            and float(row["remaining_charges"])
            > float(default_charges) + _EPSILON
        ):
            raise ContinuityError(
                "ITEM_CHARGE_CAPACITY_EXCEEDED",
                "remaining_charges exceeds the function charge capacity",
                details={
                    "remaining_charges": row["remaining_charges"],
                    "charges": default_charges,
                },
            )
        if (
            bool(row.get("enabled"))
            and str(row.get("unlock_state")) != "unlocked"
        ):
            raise ContinuityError(
                "ITEM_FUNCTION_STATE_CONFLICT",
                "enabled function runtime must be unlocked",
                details={"unlock_state": row.get("unlock_state")},
            )
        row.update(
            {
                "source_event_id": source_event_id,
                "story_coordinate_json": coordinate,
                "updated_order": int(updated_order),
                "state_json": {
                    **self._state_payload(row),
                    "last_action": action,
                },
            }
        )

    def _subject_snapshot(
        self,
        subject_type: str,
        subject_id: str,
        *,
        function_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "custody": deepcopy(self.custody.get((subject_type, subject_id))),
        }
        if subject_type == "item_instance":
            snapshot["instance"] = deepcopy(self.instances.get(subject_id))
            snapshot["runtime"] = deepcopy(self.runtime.get(subject_id))
            if function_id:
                snapshot["function_runtime"] = deepcopy(
                    self.function_runtime.get(
                        _function_runtime_key(
                            subject_type, subject_id, function_id
                        )
                    )
                )
        else:
            snapshot["stack"] = deepcopy(self.stacks.get(subject_id))
            if function_id:
                snapshot["function_runtime"] = deepcopy(
                    self.function_runtime.get(
                        _function_runtime_key(
                            subject_type, subject_id, function_id
                        )
                    )
                )
        return snapshot

    def _validate_ability_bridge(
        self,
        actor_entity_id: str,
        subject_type: str,
        subject_id: str,
        function: Mapping[str, Any],
    ) -> None:
        definition = self._definition_payload(function)
        if str(function.get("effect_owner")) != "ability_bridge":
            return
        if not self.rollout_policy.power_binding_bridge:
            raise ContinuityError(
                "ITEM_POWER_BINDING_BRIDGE_DISABLED",
                "item-to-ability bridge rollout is disabled",
                details={
                    "actor_entity_id": actor_entity_id,
                    "function_id": str(function.get("function_id") or ""),
                },
            )
        required = {
            str(value)
            for value in definition.get("granted_ability_ids") or []
        }
        definition_id, item_definition = self._definition_for_subject(
            subject_type, subject_id
        )
        sources: set[str] = set()
        if item_definition.get("item_entity_id"):
            sources.add(str(item_definition["item_entity_id"]))
        if subject_type == "item_instance":
            instance = self.instances[subject_id]
            if instance.get("item_entity_id"):
                sources.add(str(instance["item_entity_id"]))
        matching: set[str] = set()
        for binding in self.power_bindings:
            if (
                str(binding.get("actor_entity_id")) != actor_entity_id
                or str(binding.get("source_entity_id")) not in sources
            ):
                continue
            raw_abilities = binding.get("ability_entity_ids_json")
            abilities = _decode_json(raw_abilities, [])
            matching.update(str(value) for value in abilities)
        if not sources or not required.issubset(matching):
            raise ContinuityError(
                "ITEM_ABILITY_BRIDGE_INACTIVE",
                "ability-bridge item use requires an active accepted power binding",
                details={
                    "actor_entity_id": actor_entity_id,
                    "item_definition_id": definition_id,
                    "required_ability_entity_ids": sorted(required),
                    "active_ability_entity_ids": sorted(matching),
                },
            )

    def _validate_function_conditions(
        self,
        actor_entity_id: str,
        subject_type: str,
        subject_id: str,
        function: Mapping[str, Any],
    ) -> None:
        definition = self._definition_payload(function)
        condition_sources: list[dict[str, Any]] = []
        condition_sources.extend(
            self._predicate_mappings(
                definition.get("prerequisites"),
                field="function.prerequisites",
            )
        )
        condition_sources.extend(
            self._predicate_mappings(
                definition.get("conditions"),
                field="function.conditions",
            )
        )
        activation = definition.get("activation")
        if isinstance(activation, Mapping):
            condition_sources.extend(
                self._predicate_mappings(
                    activation.get("conditions"),
                    field="function.activation.conditions",
                )
            )
            condition_sources.append(
                {
                    field: activation[field]
                    for field in (
                        "requires_active",
                        "requires_equipped",
                        "requires_bound",
                    )
                    if field in activation
                }
            )
        definition_id, _ = self._definition_for_subject(
            subject_type, subject_id
        )
        for binding in self._function_binding_rows(
            subject_type, subject_id, definition_id
        ):
            if str(binding.get("function_id") or "") != str(
                function.get("function_id") or ""
            ):
                continue
            condition_sources.extend(
                self._predicate_mappings(
                    self._binding_payload(binding).get("conditions"),
                    field="binding.conditions",
                )
            )

        qualification_ids: set[str] = set()
        location_sets: list[set[str]] = []
        actor_sets: list[set[str]] = []
        known_by_actor = False
        requires_active = False
        requires_equipped = False
        requires_bound = False
        for conditions in condition_sources:
            qualification_ids.update(
                str(value)
                for value in (
                    conditions.get("qualification_entity_ids")
                    or conditions.get("qualifications")
                    or []
                )
                if str(value)
            )
            locations = {
                str(value)
                for value in conditions.get("location_entity_ids") or []
                if str(value)
            }
            if locations:
                location_sets.append(locations)
            actors = {
                str(value)
                for value in conditions.get("actor_entity_ids") or []
                if str(value)
            }
            if actors:
                actor_sets.append(actors)
            known_by_actor = known_by_actor or bool(
                conditions.get("known_by_actor")
            )
            requires_active = requires_active or bool(
                conditions.get("requires_active")
                or conditions.get("active")
            )
            requires_equipped = requires_equipped or bool(
                conditions.get("requires_equipped")
                or conditions.get("equipped")
            )
            requires_bound = requires_bound or bool(
                conditions.get("requires_bound")
                or conditions.get("bound")
            )

        missing = [
            str(qualification_id)
            for qualification_id in sorted(qualification_ids)
            if (
                actor_entity_id,
                str(qualification_id),
            )
            not in self.active_qualifications
        ]
        if missing:
            raise ContinuityError(
                "ITEM_QUALIFICATION_UNMET",
                "actor lacks an accepted qualification required by the item",
                details={"qualification_entity_ids": missing},
            )
        allowed_locations = (
            set.intersection(*location_sets) if location_sets else set()
        )
        if location_sets and not allowed_locations:
            raise ContinuityError(
                "ITEM_CONDITION_CONFLICT",
                "function and binding location conditions have no overlap",
            )
        if allowed_locations:
            actual = self.locations.get(actor_entity_id)
            if actual not in allowed_locations:
                raise ContinuityError(
                    "ITEM_LOCATION_CONDITION_UNMET",
                    "actor is not at a location allowed by the item function",
                    details={
                        "actual_location_entity_id": actual,
                        "allowed_location_entity_ids": sorted(
                            allowed_locations
                        ),
                    },
                )
        allowed_actors = set.intersection(*actor_sets) if actor_sets else set()
        if actor_sets and actor_entity_id not in allowed_actors:
            raise ContinuityError(
                "ITEM_ACTOR_CONDITION_UNMET",
                "actor is not allowed by the merged function conditions",
                details={
                    "actor_entity_id": actor_entity_id,
                    "allowed_actor_entity_ids": sorted(allowed_actors),
                },
            )
        if known_by_actor:
            known = any(
                row.get("observer_entity_id") == actor_entity_id
                and row.get("function_id") == function.get("function_id")
                and row.get("knowledge_plane")
                in {"actor_belief", "objective"}
                for row in self.observations.values()
            )
            if not known:
                raise ContinuityError(
                    "ITEM_FUNCTION_UNKNOWN_TO_ACTOR",
                    "actor has no accepted knowledge of the item function",
                    details={
                        "actor_entity_id": actor_entity_id,
                        "function_id": function.get("function_id"),
                    },
                )
        if requires_active or requires_equipped or requires_bound:
            if subject_type != "item_instance":
                raise ContinuityError(
                    "ITEM_ACTIVATION_INSTANCE_REQUIRED",
                    "active/equipped/bound conditions require an item instance",
                    details={"subject_type": subject_type},
                )
            runtime = self.runtime.get(subject_id)
            if runtime is None:
                raise ContinuityError(
                    "ITEM_RUNTIME_REQUIRED",
                    "item function conditions require instance runtime state",
                )
            if requires_active and not bool(runtime.get("active")):
                raise ContinuityError(
                    "ITEM_ACTIVE_CONDITION_UNMET",
                    "item function requires the instance to be active",
                )
            if requires_equipped and str(
                runtime.get("equipped_by_entity_id") or ""
            ) != actor_entity_id:
                raise ContinuityError(
                    "ITEM_EQUIPPED_CONDITION_UNMET",
                    "item function requires the actor to equip the instance",
                )
            if requires_bound and str(
                runtime.get("bound_actor_entity_id") or ""
            ) != actor_entity_id:
                raise ContinuityError(
                    "ITEM_BOUND_CONDITION_UNMET",
                    "item function requires the instance to be bound to the actor",
                )

    def _validate_function_activation_target_range(
        self,
        actor_entity_id: str,
        subject_type: str,
        subject_id: str,
        function: Mapping[str, Any],
        event: Mapping[str, Any],
    ) -> None:
        definition = self._definition_payload(function)
        activation_kind = self._activation_kind(definition)
        action = str(event.get("action") or "use")
        if activation_kind in {"passive", "reaction", "triggered"} and (
            action != "trigger"
        ):
            raise ContinuityError(
                "ITEM_ACTIVATION_ACTION_MISMATCH",
                f"{activation_kind} functions must be recorded with action=trigger",
                details={
                    "activation_kind": activation_kind,
                    "action": action,
                },
            )
        if activation_kind == "toggle":
            if subject_type != "item_instance":
                raise ContinuityError(
                    "ITEM_ACTIVATION_INSTANCE_REQUIRED",
                    "toggle functions require an item instance",
                )
            runtime = self.runtime.get(subject_id)
            if runtime is None or not bool(runtime.get("active")):
                raise ContinuityError(
                    "ITEM_ACTIVE_CONDITION_UNMET",
                    "toggle function requires item_runtime active=true",
                )

        targets = self._validate_target_spec(definition.get("targets") or [])
        kinds = {
            str(target if isinstance(target, str) else target.get("kind"))
            for target in targets
        }
        target_id = str(event.get("target_entity_id") or "").strip()
        if targets:
            only_none = kinds == {"none"}
            self_only = bool(kinds) and kinds.issubset({"self", "actor"})
            if only_none and target_id:
                raise ContinuityError(
                    "ITEM_TARGET_FORBIDDEN",
                    "this item function does not accept a target",
                )
            if not only_none and not self_only and not target_id:
                raise ContinuityError(
                    "ITEM_TARGET_REQUIRED",
                    "this item function requires target_entity_id",
                    details={"target_kinds": sorted(kinds)},
                )
            if self_only and target_id and target_id != actor_entity_id:
                raise ContinuityError(
                    "ITEM_TARGET_TYPE_MISMATCH",
                    "self/actor target must equal actor_entity_id",
                )
            typed_kinds = (
                kinds
                - {"none", "self", "actor", "any", "entity"}
            ) & {
                "character",
                "item",
                "location",
                "organization",
                "faction",
                *self.entity_types.values(),
            }
            if target_id and typed_kinds:
                actual_type = self.entity_types.get(target_id)
                if actual_type is None:
                    raise ContinuityError(
                        "ITEM_TARGET_NOT_FOUND",
                        "target_entity_id is not an accepted entity",
                        details={"target_entity_id": target_id},
                    )
                if actual_type not in typed_kinds:
                    raise ContinuityError(
                        "ITEM_TARGET_TYPE_MISMATCH",
                        "target entity type is not accepted by the item function",
                        details={
                            "target_entity_id": target_id,
                            "actual_entity_type": actual_type,
                            "allowed_entity_types": sorted(typed_kinds),
                        },
                    )

        range_spec = self._validate_range_spec(definition.get("range"))
        if range_spec is None:
            return
        if isinstance(range_spec, str):
            range_kind = range_spec
            range_payload: Mapping[str, Any] = {}
        else:
            range_kind = str(range_spec.get("kind") or "")
            range_payload = range_spec
        if range_kind == "self" and target_id and target_id != actor_entity_id:
            raise ContinuityError(
                "ITEM_RANGE_UNMET",
                "self range cannot address another entity",
            )
        if range_kind in {"touch", "same_location"} and target_id:
            actor_location = self.locations.get(actor_entity_id)
            target_location = self.locations.get(target_id)
            if (
                actor_location is None
                or target_location is None
                or actor_location != target_location
            ):
                raise ContinuityError(
                    "ITEM_RANGE_UNMET",
                    "actor and target are not at the same accepted location",
                    details={
                        "actor_location_entity_id": actor_location,
                        "target_location_entity_id": target_location,
                    },
                )
        allowed_locations = {
            str(value)
            for value in range_payload.get("location_entity_ids") or []
        }
        if allowed_locations:
            actual = self.locations.get(actor_entity_id)
            if actual not in allowed_locations:
                raise ContinuityError(
                    "ITEM_RANGE_UNMET",
                    "actor location falls outside the function range",
                    details={
                        "actual_location_entity_id": actual,
                        "allowed_location_entity_ids": sorted(
                            allowed_locations
                        ),
                    },
                )

    @staticmethod
    def _merge_function_costs(
        event_delta: Mapping[str, Any],
        definition: Mapping[str, Any],
        *,
        has_charge_runtime: bool,
    ) -> dict[str, float]:
        supported = {
            "quantity",
            "charges",
            "durability",
            "energy",
            "cooldown",
        }
        unsupported = sorted(set(event_delta) - supported)
        if unsupported:
            raise ContinuityError(
                "ITEM_DELTA_FIELD_UNSUPPORTED",
                "item use delta contains unsupported fields",
                details={"fields": unsupported},
            )
        costs = {
            str(key): _finite_number(
                value,
                field=f"delta.{key}",
                minimum=0,
            )
            for key, value in event_delta.items()
        }
        if (
            "charges" not in costs
            and has_charge_runtime
            and definition.get("charges") is not None
        ):
            costs["charges"] = 1.0
        if (
            "durability" not in costs
            and definition.get("durability_cost") is not None
        ):
            costs["durability"] = _finite_number(
                definition["durability_cost"],
                field="function.durability_cost",
                minimum=0,
            )
        for cost in definition.get("costs") or []:
            if not isinstance(cost, Mapping):
                continue
            kind = str(cost.get("kind") or cost.get("resource") or "")
            if kind not in {
                "quantity",
                "charges",
                "durability",
                "energy",
            }:
                continue
            if kind in costs:
                continue
            amount = cost.get("amount")
            if amount is not None:
                costs[kind] = _finite_number(
                    amount,
                    field=f"function.costs.{kind}",
                    minimum=0,
                )
        if "cooldown" not in costs and isinstance(
            definition.get("cooldown"), (int, float)
        ):
            costs["cooldown"] = _finite_number(
                definition["cooldown"],
                field="function.cooldown",
                minimum=0,
            )
        return costs

    def _apply_item_use(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str,
        updated_order: int,
    ) -> None:
        coordinate = self._require_story_coordinate(event)
        subject_type, subject_id = _subject(event)
        function_id = str(event.get("function_id") or "")
        actor = str(event.get("actor_entity_id") or "").strip()
        if not actor:
            raise ContinuityError(
                "ITEM_ACTOR_REQUIRED",
                "item use requires actor_entity_id",
            )
        function = self._function_for_subject(
            subject_type, subject_id, function_id
        )
        if not self._actor_can_access(actor, subject_type, subject_id):
            raise ContinuityError(
                "ITEM_ACCESS_DENIED",
                "actor has no accepted custody, ownership, or access to use the item",
                details={
                    "actor_entity_id": actor,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                },
            )
        self._validate_actor_location(actor, subject_type, subject_id)
        self._validate_function_conditions(
            actor, subject_type, subject_id, function
        )
        self._validate_function_activation_target_range(
            actor,
            subject_type,
            subject_id,
            function,
            event,
        )
        self._validate_ability_bridge(
            actor, subject_type, subject_id, function
        )

        runtime: dict[str, Any] | None = None
        if subject_type == "item_instance":
            runtime = self._ensure_instance_runtime(
                subject_id,
                source_event_id=source_event_id,
                updated_order=updated_order,
                coordinate=coordinate,
            )
            if bool(runtime.get("destroyed")):
                raise ContinuityError(
                    "ITEM_DESTROYED",
                    "destroyed item cannot be used",
                )
            if bool(runtime.get("sealed")):
                raise ContinuityError(
                    "ITEM_SEALED",
                    "sealed item cannot be used",
                )
            if (
                runtime.get("durability") is not None
                and float(runtime["durability"]) <= _EPSILON
            ):
                raise ContinuityError(
                    "ITEM_BROKEN",
                    "item has no remaining durability",
                )
        function_runtime = self._ensure_function_runtime(
            subject_type,
            subject_id,
            function_id,
            source_event_id=source_event_id,
            updated_order=updated_order,
            coordinate=coordinate,
        )
        if (
            not bool(function_runtime.get("enabled"))
            or str(function_runtime.get("unlock_state")) != "unlocked"
        ):
            raise ContinuityError(
                "ITEM_FUNCTION_LOCKED",
                "item function is disabled, locked, or suppressed",
                details={
                    "function_id": function_id,
                    "unlock_state": function_runtime.get("unlock_state"),
                },
            )
        cooldown_until = _coordinate(
            function_runtime.get("cooldown_until_json")
        )
        if not _cooldown_ready(coordinate, cooldown_until):
            raise ContinuityError(
                "ITEM_FUNCTION_ON_COOLDOWN",
                "item function cooldown has not completed",
                details={"cooldown_until": cooldown_until},
            )

        before = self._subject_snapshot(
            subject_type,
            subject_id,
            function_id=function_id,
        )
        function_definition = self._definition_payload(function)
        costs = self._merge_function_costs(
            dict(event.get("delta") or {}),
            function_definition,
            has_charge_runtime=(
                function_runtime is not None
                and function_runtime.get("remaining_charges") is not None
            ),
        )

        quantity_cost = float(costs.get("quantity", 0.0))
        if str(event.get("action") or "") == "consume" and quantity_cost <= 0:
            quantity_cost = 1.0
        if quantity_cost > 0:
            if subject_type != "item_stack":
                if quantity_cost > 1.0 + _EPSILON:
                    raise ContinuityError(
                        "ITEM_INVALID_QUANTITY",
                        "an item instance can only be consumed once",
                    )
            else:
                stack = self.stacks[subject_id]
                available = float(stack["quantity"])
                if available + _EPSILON < quantity_cost:
                    raise ContinuityError(
                        "ITEM_INSUFFICIENT_QUANTITY",
                        "item stack has insufficient quantity",
                        details={
                            "available": available,
                            "requested": quantity_cost,
                        },
                    )
                stack["quantity"] = _finite_number(
                    available - quantity_cost,
                    field="item_stack.quantity",
                    minimum=0,
                )
                stack["source_event_id"] = source_event_id
                stack["story_coordinate_json"] = coordinate
                stack["updated_order"] = int(updated_order)
                custody = self.custody.get(("item_stack", subject_id))
                if stack["quantity"] <= _EPSILON:
                    stack["quantity"] = 0.0
                    stack["stack_status"] = "depleted"
                    self.custody.pop(("item_stack", subject_id), None)
                elif custody is not None:
                    custody["quantity"] = float(stack["quantity"])
                    custody["source_event_id"] = source_event_id
                    custody["story_coordinate_json"] = coordinate
                    custody["updated_order"] = int(updated_order)

        charge_cost = float(costs.get("charges", 0.0))
        if charge_cost > 0:
            remaining = function_runtime.get("remaining_charges")
            if remaining is None:
                raise ContinuityError(
                    "ITEM_CHARGES_UNMODELED",
                    "item function has no modeled charges",
                )
            if float(remaining) + _EPSILON < charge_cost:
                raise ContinuityError(
                    "ITEM_INSUFFICIENT_CHARGES",
                    "item function has insufficient remaining charges",
                    details={
                        "available": remaining,
                        "requested": charge_cost,
                    },
                )
            function_runtime["remaining_charges"] = _finite_number(
                float(remaining) - charge_cost,
                field="function.remaining_charges",
                minimum=0,
            )
        cooldown_cost = costs.get("cooldown")
        if cooldown_cost is not None:
            function_runtime["cooldown_until_json"] = _add_cooldown(
                coordinate, cooldown_cost
            )
        function_runtime.update(
            {
                "source_event_id": source_event_id,
                "story_coordinate_json": coordinate,
                "updated_order": int(updated_order),
                "state_json": {
                    **self._state_payload(function_runtime),
                    "last_used_coordinate": coordinate,
                },
            }
        )

        if runtime is None and any(
            float(costs.get(field, 0.0)) > 0
            for field in ("durability", "energy")
        ):
            raise ContinuityError(
                "ITEM_STACK_RUNTIME_COST_UNSUPPORTED",
                "stack-bound functions cannot consume instance durability or energy",
                details={
                    "durability": costs.get("durability", 0.0),
                    "energy": costs.get("energy", 0.0),
                },
            )
        if runtime is not None:
            durability_cost = float(costs.get("durability", 0.0))
            if durability_cost > 0:
                durability = runtime.get("durability")
                if durability is None:
                    raise ContinuityError(
                        "ITEM_DURABILITY_UNMODELED",
                        "item has no modeled durability",
                    )
                if float(durability) + _EPSILON < durability_cost:
                    raise ContinuityError(
                        "ITEM_INSUFFICIENT_DURABILITY",
                        "item has insufficient remaining durability",
                        details={
                            "available": durability,
                            "requested": durability_cost,
                        },
                    )
                runtime["durability"] = _finite_number(
                    float(durability) - durability_cost,
                    field="item.durability",
                    minimum=0,
                )
                runtime["damaged"] = 1
                if float(runtime["durability"]) <= _EPSILON:
                    runtime["active"] = 0
            energy_cost = float(costs.get("energy", 0.0))
            if energy_cost > 0:
                energy = runtime.get("energy")
                if energy is None:
                    raise ContinuityError(
                        "ITEM_ENERGY_UNMODELED",
                        "item has no modeled energy",
                    )
                if float(energy) + _EPSILON < energy_cost:
                    raise ContinuityError(
                        "ITEM_INSUFFICIENT_ENERGY",
                        "item has insufficient remaining energy",
                        details={
                            "available": energy,
                            "requested": energy_cost,
                        },
                    )
                runtime["energy"] = _finite_number(
                    float(energy) - energy_cost,
                    field="item.energy",
                    minimum=0,
                )
            runtime.update(
                {
                    "source_event_id": source_event_id,
                    "story_coordinate_json": coordinate,
                    "updated_order": int(updated_order),
                    "state_json": {
                        **self._state_payload(runtime),
                        "last_used_coordinate": coordinate,
                    },
                }
            )
            if (
                str(event.get("action") or "") == "consume"
                or quantity_cost > 0
            ):
                self._destroy_instance(
                    subject_id,
                    source_event_id=source_event_id,
                    updated_order=updated_order,
                    coordinate=coordinate,
                    consumed=True,
                )

        if (
            subject_type == "item_stack"
            and str(self.stacks[subject_id].get("stack_status")) == "depleted"
        ):
            self._cleanup_subject_runtime(
                "item_stack",
                subject_id,
                reason="stack_depleted",
                source_event_id=source_event_id,
                updated_order=updated_order,
                coordinate=coordinate,
                zero_charges=True,
            )

        self._validate_container_graph()
        after = self._subject_snapshot(
            subject_type,
            subject_id,
            function_id=function_id,
        )
        history_delta = {
            "schema_version": ITEM_DELTA_SCHEMA_VERSION,
            "requested": dict(event.get("delta") or {}),
            "applied": costs,
        }
        for context_field in ("location_entity_id", "resource_entity_id"):
            if event.get(context_field) is not None:
                history_delta[context_field] = event[context_field]
        self.use_history[source_event_id] = {
            "source_event_id": source_event_id,
            "item_instance_id": (
                subject_id if subject_type == "item_instance" else None
            ),
            "stack_id": (
                subject_id if subject_type == "item_stack" else None
            ),
            "function_id": function_id,
            "actor_entity_id": actor,
            "target_entity_id": event.get("target_entity_id"),
            "action": str(event.get("action") or "use"),
            "delta_json": history_delta,
            "before_json": before,
            "after_json": after,
            "story_coordinate_json": coordinate,
            "chapter_no": event.get("chapter_no"),
            "scene_index": event.get("scene_index"),
            "updated_order": int(updated_order),
        }

    def _apply_item_observation(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str,
        updated_order: int,
    ) -> None:
        subject_type, subject_id = _observation_subject(event)
        if subject_type == "item_definition":
            definition = self.definitions.get(subject_id)
            if definition is None:
                raise ContinuityError(
                    "ITEM_DEFINITION_NOT_FOUND",
                    f"unknown item definition: {subject_id}",
                )
            definition_id = subject_id
        else:
            definition_id, _ = self._definition_for_subject(
                subject_type, subject_id
            )
        function_id = (
            str(event.get("function_id"))
            if event.get("function_id") is not None
            else None
        )
        if function_id:
            if subject_type == "item_definition":
                function = self.functions.get(function_id)
                if (
                    function is None
                    or str(function.get("item_definition_id"))
                    != definition_id
                ):
                    raise ContinuityError(
                        "ITEM_FUNCTION_DEFINITION_MISMATCH",
                        "observed function does not belong to the item definition",
                        details={
                            "item_definition_id": definition_id,
                            "function_id": function_id,
                        },
                    )
            else:
                self._function_for_subject(
                    subject_type,
                    subject_id,
                    function_id,
                    require_active_binding=False,
                )
        knowledge_plane = _require_enum(
            event.get("knowledge_plane") or "actor_belief",
            field="knowledge_plane",
            choices=ITEM_KNOWLEDGE_PLANES,
        )
        observer = event.get("observer_entity_id")
        if observer is not None:
            observer = str(observer).strip() or None
        if knowledge_plane == "actor_belief" and observer is None:
            raise ContinuityError(
                "ITEM_OBSERVER_REQUIRED",
                "actor_belief item observations require observer_entity_id",
            )
        action = _require_enum(
            event.get("action") or "observe",
            field="action",
            choices={
                "observe",
                "reveal",
                "claim",
                "misidentify",
                "correct",
            },
        )
        observation = event.get("observation") or {}
        if not isinstance(observation, Mapping) or not observation:
            raise ContinuityError(
                "ITEM_OBSERVATION_REQUIRED",
                "item observation payload must be a non-empty object",
            )
        coordinate = self._story_coordinate(event)
        observation_key = stable_item_id(
            "item_observation_",
            {
                "source_event_id": source_event_id,
                "observer_entity_id": observer,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "function_id": function_id,
                "knowledge_plane": event.get("knowledge_plane"),
            },
        )
        observation_payload = {
            "observation": dict(observation),
            "confidence": event.get("confidence", 1.0),
            "scope": event.get("scope"),
            "branch_id": event.get("branch_id"),
        }
        for context_field in ("source_entity_id", "target_entity_id"):
            if event.get(context_field) is not None:
                observation_payload[context_field] = event[context_field]
        self.observations[observation_key] = {
            "observation_key": observation_key,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "observer_entity_id": observer,
            "item_definition_id": (
                subject_id if subject_type == "item_definition" else None
            ),
            "item_instance_id": (
                subject_id if subject_type == "item_instance" else None
            ),
            "stack_id": (
                subject_id if subject_type == "item_stack" else None
            ),
            "function_id": function_id,
            "observation_action": action,
            "knowledge_plane": knowledge_plane,
            "observation_json": observation_payload,
            "source_event_id": source_event_id,
            "story_coordinate_json": dict(coordinate or {}),
            "updated_order": int(updated_order),
        }

    def apply(
        self,
        event: Mapping[str, Any],
        *,
        source_event_id: str,
        updated_order: int,
    ) -> None:
        payload = dict(event)
        event_type = str(payload.get("event_type") or "")
        if event_type == "item_correction":
            if str(payload.get("action") or "") == "retract":
                return
            replacement = payload.get("replacement")
            if not isinstance(replacement, Mapping):
                return
            payload = dict(replacement)
            event_type = str(payload.get("event_type") or "")
        if event_type not in ITEM_EVENT_TYPES - {"item_correction"}:
            return

        scope = str(payload.get("scope") or "current")
        is_flashback = str(payload.get("narrative_mode") or "linear") == (
            "flashback"
        )
        if scope == "planned":
            return
        if (scope == "historical" or is_flashback) and event_type not in {
            "item_observation",
        }:
            return

        if event_type == "item_spec":
            self._apply_item_spec(
                payload,
                source_event_id=source_event_id,
                updated_order=updated_order,
            )
        elif event_type == "item_instance":
            self._apply_item_instance(
                payload,
                source_event_id=source_event_id,
                updated_order=updated_order,
            )
        elif event_type == "item_custody":
            self._apply_item_custody(
                payload,
                source_event_id=source_event_id,
                updated_order=updated_order,
            )
        elif event_type == "item_runtime":
            self._apply_item_runtime(
                payload,
                source_event_id=source_event_id,
                updated_order=updated_order,
            )
        elif event_type == "item_function_runtime":
            self._apply_item_function_runtime(
                payload,
                source_event_id=source_event_id,
                updated_order=updated_order,
            )
        elif event_type == "item_use":
            self._apply_item_use(
                payload,
                source_event_id=source_event_id,
                updated_order=updated_order,
            )
        elif event_type == "item_observation":
            self._apply_item_observation(
                payload,
                source_event_id=source_event_id,
                updated_order=updated_order,
            )

    def apply_sequence(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        source_prefix: str,
        updated_order_start: int,
    ) -> None:
        for index, event in enumerate(events):
            self.apply(
                event,
                source_event_id=f"{source_prefix}{index}",
                updated_order=int(updated_order_start) + index,
            )


def _normalized_item_events(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        dict(event)
        for event in events
        if str(event.get("event_type") or "") in ITEM_EVENT_TYPES
    ]


def _validate_item_event_contract(
    connection: sqlite3.Connection,
    item_events: Sequence[Mapping[str, Any]],
) -> None:
    for event in item_events:
        declared_schema = event.get("schema_version")
        if (
            declared_schema is not None
            and str(declared_schema) != ITEM_DELTA_SCHEMA_VERSION
        ):
            raise ContinuityError(
                "ITEM_DELTA_SCHEMA_MISMATCH",
                "schema-v6 item events require plot-rag-delta/v4",
                details={"schema_version": declared_schema},
            )
        if str(event.get("event_type")) != "item_correction":
            continue
        target_event_id = str(event.get("target_event_id") or "")
        target = connection.execute(
            """
            SELECT event_type
            FROM continuity_events
            WHERE event_id=?
            """,
            (target_event_id,),
        ).fetchone()
        if target is None:
            raise ContinuityError(
                "EVENT_LINK_TARGET_NOT_FOUND",
                "item correction target does not exist",
                details={"target_event_id": target_event_id},
            )
        if str(target["event_type"]) not in ITEM_EVENT_TYPES:
            raise ContinuityError(
                "ITEM_CORRECTION_TARGET_INVALID",
                "item correction may only target an accepted item event",
                details={
                    "target_event_id": target_event_id,
                    "event_type": str(target["event_type"]),
                },
            )


def inspect_item_event_sequence(
    connection: sqlite3.Connection,
    events: Sequence[Mapping[str, Any]],
    *,
    rollout_policy: ItemRolloutPolicy | None = None,
) -> dict[str, Any]:
    """Run the strict reducer locally and return shadow diagnostics.

    Every event is applied to a cloned in-memory state.  A failed event leaves
    the prior candidate state untouched, which preserves the zero-write
    semantics while allowing later independent events to be inspected.
    """

    policy = rollout_policy or ItemRolloutPolicy()
    item_events = _normalized_item_events(events)
    report: dict[str, Any] = {
        "mode": (
            "strict"
            if policy.strict_runtime_validation
            else "shadow"
        ),
        "policy": policy.as_dict(),
        "event_count": len(item_events),
        "applied_event_count": 0,
        "diagnostic_count": 0,
        "diagnostics": [],
        "status": "passed",
    }
    if not item_events:
        return report

    try:
        _validate_item_event_contract(connection, item_events)
    except ContinuityError as exc:
        diagnostic = {
            "event_index": None,
            "event_type": None,
            "code": exc.code,
            "message": str(exc),
            "details": dict(exc.details),
        }
        report["diagnostics"] = [diagnostic]
        report["diagnostic_count"] = 1
        report["status"] = "differences"
        return report

    state = ItemProjectionState.from_connection(
        connection,
        rollout_policy=policy,
    )
    head_row = connection.execute(
        "SELECT value FROM state_meta WHERE key='head_canon_revision'"
    ).fetchone()
    head = int(head_row[0]) if head_row is not None else 0
    for index, event in enumerate(item_events):
        candidate = deepcopy(state)
        try:
            candidate.apply(
                event,
                source_event_id=f"shadow_item_event_{index}",
                updated_order=(head + 1) * 1_000_000 + index,
            )
        except ContinuityError as exc:
            report["diagnostics"].append(
                {
                    "event_index": index,
                    "event_type": str(event.get("event_type") or ""),
                    "code": exc.code,
                    "message": str(exc),
                    "details": dict(exc.details),
                }
            )
            continue
        state = candidate
        report["applied_event_count"] += 1
    report["diagnostic_count"] = len(report["diagnostics"])
    if report["diagnostic_count"]:
        report["status"] = "differences"
    return report


def _bridge_event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    if str(payload.get("event_type") or "") != "item_correction":
        return payload
    if str(payload.get("action") or "") == "retract":
        return payload
    replacement = payload.get("replacement")
    return dict(replacement) if isinstance(replacement, Mapping) else payload


def _accepted_bridge_function_ids(
    connection: sqlite3.Connection,
) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT function_id
            FROM item_function_definitions
            WHERE function_status='active' AND effect_owner='ability_bridge'
            """
        )
    }


def _item_source_entity_ids(
    connection: sqlite3.Connection,
    events: Sequence[Mapping[str, Any]],
) -> set[str]:
    proposed = {
        str(event.get("entity_id") or "")
        for event in events
        if str(event.get("event_type") or "") == "entity"
        and str(event.get("entity_type") or "") == "item"
        and event.get("entity_id")
    }
    persisted = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT entity_id FROM entities WHERE entity_type='item'
            UNION
            SELECT item_entity_id
            FROM item_definitions
            WHERE item_entity_id IS NOT NULL
            UNION
            SELECT item_entity_id
            FROM item_instances
            WHERE item_entity_id IS NOT NULL
            """
        )
    }
    return proposed | persisted


def detect_item_ability_bridge_attempts(
    connection: sqlite3.Connection,
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return v4 item-to-ability bridge creations or activations.

    Cleanup operations remain available while the bridge rollout is disabled:
    deprecating a function, suppressing a function, and unbinding an existing
    power binding are not classified as new bridge application.
    """

    bridge_functions = _accepted_bridge_function_ids(connection)
    for raw_event in events:
        event = _bridge_event_payload(raw_event)
        if (
            str(event.get("event_type") or "") == "item_spec"
            and str(event.get("spec_type") or "") == "function_definition"
            and str(event.get("action") or "define") != "deprecate"
        ):
            definition = dict(event.get("definition") or {})
            if (
                str(definition.get("effect_owner") or "inline")
                == "ability_bridge"
                or bool(definition.get("granted_ability_ids"))
            ):
                bridge_functions.add(
                    str(
                        event.get("function_id")
                        or event.get("spec_id")
                        or definition.get("function_id")
                        or ""
                    )
                )

    item_sources = _item_source_entity_ids(connection, events)
    attempts: list[dict[str, Any]] = []
    for index, raw_event in enumerate(events):
        event = _bridge_event_payload(raw_event)
        event_type = str(event.get("event_type") or "")
        action = str(event.get("action") or "")
        function_id = str(
            event.get("function_id")
            or (event.get("definition") or {}).get("function_id")
            or ""
        )
        reason: str | None = None
        if event_type == "item_spec":
            spec_type = str(event.get("spec_type") or "")
            definition = dict(event.get("definition") or {})
            if spec_type == "function_definition" and action != "deprecate":
                if (
                    str(definition.get("effect_owner") or "inline")
                    == "ability_bridge"
                    or bool(definition.get("granted_ability_ids"))
                ):
                    reason = "function_definition"
            elif (
                spec_type == "function_binding"
                and action != "deprecate"
                and function_id in bridge_functions
            ):
                reason = "function_binding"
        elif event_type == "item_use" and function_id in bridge_functions:
            reason = "function_use"
        elif (
            event_type == "item_runtime"
            and action == "unlock_function"
            and function_id in bridge_functions
        ):
            reason = "function_unlock"
        elif (
            event_type == "item_function_runtime"
            and action in {"enable", "unlock"}
            and function_id in bridge_functions
        ):
            reason = "function_unlock"
        elif (
            event_type == "item_function_runtime"
            and action == "bootstrap"
            and event.get("enabled") is True
            and function_id in bridge_functions
        ):
            reason = "function_runtime_bootstrap"
        elif (
            event_type == "power_binding"
            and action in {"bind", "equip", "contract", "summon"}
            and str(event.get("source_entity_id") or "") in item_sources
            and bool(event.get("ability_entity_ids"))
        ):
            reason = "item_power_binding"
        if reason is None:
            continue
        attempts.append(
            {
                "event_index": index,
                "event_type": event_type,
                "action": action,
                "function_id": function_id or None,
                "source_entity_id": event.get("source_entity_id"),
                "reason": reason,
            }
        )
    return attempts


def assert_item_rollout_acceptance(
    connection: sqlite3.Connection,
    events: Sequence[Mapping[str, Any]],
    *,
    rollout_policy: ItemRolloutPolicy,
    changes_authority: bool,
) -> dict[str, Any]:
    """Enforce production rollout gates before an approval grant is consumed."""

    bridge_attempts = detect_item_ability_bridge_attempts(connection, events)
    if bridge_attempts and not rollout_policy.power_binding_bridge:
        raise ContinuityError(
            "ITEM_POWER_BINDING_BRIDGE_DISABLED",
            "item-to-ability bridge rollout is disabled for this project",
            details={"attempts": bridge_attempts},
        )

    item_events = _normalized_item_events(events)
    if (
        item_events
        and changes_authority
        and not rollout_policy.strict_runtime_validation
    ):
        raise ContinuityError(
            "ITEM_STRICT_RUNTIME_DISABLED",
            "v4 item authority remains shadow-only until strict runtime validation is enabled",
            details={
                "event_count": len(item_events),
                "policy": rollout_policy.as_dict(),
            },
        )
    if item_events and rollout_policy.strict_runtime_validation:
        return validate_item_event_sequence(
            connection,
            events,
            rollout_policy=rollout_policy,
        )
    return inspect_item_event_sequence(
        connection,
        events,
        rollout_policy=rollout_policy,
    )


def validate_item_event_sequence(
    connection: sqlite3.Connection,
    events: Sequence[Mapping[str, Any]],
    *,
    rollout_policy: ItemRolloutPolicy | None = None,
) -> dict[str, Any]:
    """Strictly dry-run item events against the accepted projection.

    This function performs no SQL writes and raises on the first deterministic
    reducer failure.  Call :func:`inspect_item_event_sequence` for shadow mode.
    """

    policy = rollout_policy or STRICT_ITEM_ROLLOUT_POLICY
    # Keep the public strict adapter visibly bound to both frozen v4
    # constants.  The release gate audits this exact function so a future
    # refactor cannot silently disconnect schema selection from event routing.
    item_events = [
        dict(event)
        for event in events
        if str(event.get("event_type") or "") in ITEM_EVENT_TYPES
    ]
    if not item_events:
        return {
            "mode": "strict",
            "policy": policy.as_dict(),
            "event_count": 0,
            "applied_event_count": 0,
            "diagnostic_count": 0,
            "diagnostics": [],
            "status": "passed",
        }
    if any(
        event.get("schema_version") not in {None, ITEM_DELTA_SCHEMA_VERSION}
        for event in item_events
    ):
        _validate_item_event_contract(connection, item_events)
    _validate_item_event_contract(connection, item_events)
    state = ItemProjectionState.from_connection(
        connection,
        rollout_policy=policy,
    )
    head_row = connection.execute(
        "SELECT value FROM state_meta WHERE key='head_canon_revision'"
    ).fetchone()
    head = int(head_row[0]) if head_row is not None else 0
    state.apply_sequence(
        item_events,
        source_prefix="validation_item_event_",
        updated_order_start=(head + 1) * 1_000_000,
    )
    return {
        "mode": "strict",
        "policy": policy.as_dict(),
        "event_count": len(item_events),
        "applied_event_count": len(item_events),
        "diagnostic_count": 0,
        "diagnostics": [],
        "status": "passed",
    }


def rebuild_item_projection(
    connection: sqlite3.Connection,
    event_rows: Sequence[Mapping[str, Any]],
    inactive_event_ids: set[str] | frozenset[str],
    *,
    record_run: bool = True,
) -> dict[str, Any]:
    """Rebuild the independent item projection from accepted events."""

    for table in _ITEM_TABLE_DELETE_ORDER:
        if (
            table in _OPTIONAL_ITEM_PROJECTION_TABLES
            and not _table_columns(connection, table)
        ):
            continue
        connection.execute(f"DELETE FROM {table}")
    seed_legacy_item_projection(connection)
    state = ItemProjectionState.from_connection(connection)
    item_event_count = 0
    latest_event_id: str | None = None
    latest_order = 0
    for raw_row in event_rows:
        row = dict(raw_row)
        event_id = str(row.get("event_id") or "")
        if event_id in inactive_event_ids:
            continue
        if not bool(row.get("changes_authority")):
            continue
        event_type, payload = _event_payload(row)
        if event_type not in ITEM_EVENT_TYPES - {"item_correction"}:
            continue
        if not payload:
            continue
        item_event_count += 1
        latest_event_id = event_id
        latest_order = int(row.get("updated_order") or 0)
        state.apply(
            payload,
            source_event_id=event_id,
            updated_order=latest_order,
        )
    state.persist(connection)
    projection_hash = refresh_item_projection_metadata(
        connection,
        source_event_id=latest_event_id,
        updated_order=latest_order,
    )
    head = _meta_int(connection, "head_canon_revision")
    active = _meta_int(connection, "active_canon_revision")
    run_id: str | None = None
    if record_run:
        run_id = stable_item_id(
            "item_projection_run_",
            {
                "head": head,
                "active": active,
                "projection_hash": projection_hash,
            },
        )
        now_row = connection.execute(
            "SELECT value FROM state_meta WHERE key='continuity_schema_updated_at'"
        ).fetchone()
        created_at = (
            str(now_row[0])
            if now_row is not None
            else "1970-01-01T00:00:00+00:00"
        )
        # projection_runs is an audit/control table and is deliberately
        # excluded from both continuity and item projection hashes.
        connection.execute(
            """
            INSERT OR REPLACE INTO projection_runs(
                run_id, projection_name, source_head_revision,
                source_active_revision, run_status, projection_hash,
                details_json, created_at, completed_at
            ) VALUES(?, 'items', ?, ?, 'completed', ?, ?, ?, ?)
            """,
            (
                run_id,
                head,
                active,
                projection_hash,
                canonical_json(
                    {
                        "schema_version": ITEM_PROJECTION_SCHEMA_VERSION,
                        "event_count": item_event_count,
                        "inactive_event_count": len(inactive_event_ids),
                    }
                ),
                created_at,
                created_at,
            ),
        )
    return {
        "item_projection_hash": projection_hash,
        "item_projection_schema_version": ITEM_PROJECTION_SCHEMA_VERSION,
        "item_event_count": item_event_count,
        "item_projection_run_id": run_id,
    }
