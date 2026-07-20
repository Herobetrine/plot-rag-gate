"""SQLite schema for the canonical lifecycle and continuity engine.

The existing plug-in ships a schema-v2 state database.  The continuity
package deliberately leaves those legacy tables intact and adds a v6 ledger
beside them.  That lets the old reader continue to open the database while
new callers use immutable lifecycle commits and replayable projections.  The
v6 item and control-plane tables are strictly additive to the v5 schema.
"""

from __future__ import annotations

import re

LEGACY_SCHEMA_VERSION = 2
SCHEMA_VERSION = 7
ITEM_PROJECTION_SCHEMA_VERSION = 1


class SchemaVersionError(ValueError):
    """Raised when persisted continuity version metadata is not admissible."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(f"{self.code}: {message}")


def validate_schema_versions(
    *,
    user_tables_present: bool,
    legacy_version: int,
    continuity_version: int,
) -> None:
    """Validate the shared legacy/continuity schema-version contract.

    A truly empty SQLite database may begin at ``(0, 0)``. Every existing
    continuity database must retain the legacy-v2 marker, while continuity
    versions zero through the current version remain migration candidates.
    The function is intentionally side-effect free so write migrations and
    dry-run diagnostics enforce the same gate.
    """

    if legacy_version < 0 or continuity_version < 0:
        raise SchemaVersionError(
            "STATE_SCHEMA_UNREADABLE",
            "schema versions must be non-negative",
        )
    if legacy_version > LEGACY_SCHEMA_VERSION:
        raise SchemaVersionError(
            "STATE_LEGACY_SCHEMA_TOO_NEW",
            f"stored={legacy_version}, supported={LEGACY_SCHEMA_VERSION}",
        )
    if legacy_version not in {0, LEGACY_SCHEMA_VERSION}:
        raise SchemaVersionError(
            "STATE_LEGACY_SCHEMA_UNSUPPORTED",
            f"stored={legacy_version}, supported={LEGACY_SCHEMA_VERSION}",
        )
    if continuity_version > SCHEMA_VERSION:
        raise SchemaVersionError(
            "STATE_SCHEMA_TOO_NEW",
            f"stored={continuity_version}, supported={SCHEMA_VERSION}",
        )
    if user_tables_present and legacy_version == 0:
        if continuity_version > 0:
            message = (
                "existing continuity database has no supported legacy "
                "schema version"
            )
        else:
            message = (
                "existing database contains user tables but no readable "
                "schema version"
            )
        raise SchemaVersionError("STATE_SCHEMA_VERSION_MISSING", message)


ARTIFACT_STAGES = (
    "bootstrap",
    "brainstorm",
    "outline",
    "draft",
    "final",
    "published",
)

CANON_STATUSES = ("proposed", "accepted", "rejected", "retracted")
FACT_SCOPES = ("current", "planned", "historical", "timeless")
SOURCE_ROLES = ("canon", "setting", "outline", "draft", "note", "reference")

EVENT_TYPES = (
    "fact",
    "state",
    "entity",
    "world_rule",
    "relation",
    "movement",
    "inventory",
    "item_spec",
    "item_instance",
    "item_custody",
    "item_runtime",
    "item_function_runtime",
    "item_use",
    "item_observation",
    "item_correction",
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
    "ability",
    "power_spec",
    "progression",
    "resource",
    "status_effect",
    "power_binding",
    "qualification",
    "power_observation",
    "belief",
    "open_loop",
    "time",
    "correction",
    "retraction",
)


# A fresh database must remain readable by the pre-v4 state_rag module.
LEGACY_V2_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    receipt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    prompt_hash TEXT NOT NULL,
    assistant_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    retrieved_json TEXT NOT NULL DEFAULT '[]',
    authority_json TEXT NOT NULL DEFAULT '{}',
    craft_json TEXT NOT NULL DEFAULT '{}',
    remote_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS state_events (
    event_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL,
    subject TEXT NOT NULL,
    field TEXT NOT NULL,
    operation TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'current',
    effective_at TEXT,
    value_json TEXT,
    confidence REAL NOT NULL,
    evidence TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(receipt_id) REFERENCES turns(receipt_id)
);

CREATE TABLE IF NOT EXISTS current_facts (
    fact_key TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    subject TEXT NOT NULL,
    field TEXT NOT NULL,
    value_json TEXT NOT NULL,
    event_id TEXT NOT NULL,
    effective_at TEXT,
    confidence REAL NOT NULL,
    evidence TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES state_events(event_id)
);

CREATE TABLE IF NOT EXISTS fact_vectors (
    fact_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(fact_key) REFERENCES current_facts(fact_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS turn_commits (
    request_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    deltas_json TEXT NOT NULL,
    craft_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(receipt_id) REFERENCES turns(receipt_id)
);

CREATE INDEX IF NOT EXISTS idx_events_request ON state_events(request_id);
CREATE INDEX IF NOT EXISTS idx_events_subject ON state_events(subject, category);
CREATE INDEX IF NOT EXISTS idx_facts_subject ON current_facts(subject, category);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, started_at);
"""


CONTINUITY_V5_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_type_name
    ON entities(entity_type, normalized_name);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    alias_text TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    alias_status TEXT NOT NULL DEFAULT 'confirmed',
    source_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(entity_id) REFERENCES entities(entity_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_alias_entity_text
    ON entity_aliases(entity_id, normalized_alias);
CREATE INDEX IF NOT EXISTS idx_alias_lookup
    ON entity_aliases(normalized_alias, alias_status, confidence DESC);

CREATE TABLE IF NOT EXISTS mention_resolutions (
    resolution_id TEXT PRIMARY KEY,
    artifact_id TEXT,
    mention_text TEXT NOT NULL,
    normalized_mention TEXT NOT NULL,
    entity_id TEXT,
    resolution_status TEXT NOT NULL,
    candidates_json TEXT NOT NULL DEFAULT '[]',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(entity_id) REFERENCES entities(entity_id)
);

CREATE INDEX IF NOT EXISTS idx_mentions_artifact
    ON mention_resolutions(artifact_id, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_version_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    artifact_stage TEXT NOT NULL,
    canon_status TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    chapter_no INTEGER,
    scene_index INTEGER,
    artifact_revision INTEGER NOT NULL,
    source_role TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(artifact_id, branch_id, artifact_revision)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_story_point
    ON artifacts(branch_id, chapter_no, scene_index, artifact_revision);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    artifact_version_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    artifact_stage TEXT NOT NULL,
    canon_status TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    chapter_no INTEGER,
    scene_index INTEGER,
    artifact_revision INTEGER NOT NULL,
    prepared_canon_revision INTEGER NOT NULL,
    source_role TEXT NOT NULL,
    proposal_kind TEXT NOT NULL DEFAULT 'story_delta',
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    events_json TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    status_reason TEXT NOT NULL DEFAULT '',
    accepted_commit_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(artifact_version_id) REFERENCES artifacts(artifact_version_id)
);

CREATE INDEX IF NOT EXISTS idx_proposals_status
    ON proposals(canon_status, validation_status, created_at);
CREATE INDEX IF NOT EXISTS idx_proposals_artifact
    ON proposals(artifact_id, branch_id, artifact_revision);

CREATE TABLE IF NOT EXISTS proposal_issues (
    issue_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    issue_code TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(proposal_id) REFERENCES proposals(proposal_id)
);

CREATE INDEX IF NOT EXISTS idx_proposal_issues
    ON proposal_issues(proposal_id, severity, issue_code);

CREATE TABLE IF NOT EXISTS approval_grants (
    token_hash TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    binding_json TEXT NOT NULL,
    authorized_operations_json TEXT NOT NULL,
    expected_canon_revision INTEGER NOT NULL,
    issuer TEXT NOT NULL,
    channel TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_request_hash TEXT,
    accepted_commit_id TEXT,
    consumed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(proposal_id) REFERENCES proposals(proposal_id)
);

CREATE INDEX IF NOT EXISTS idx_grants_proposal
    ON approval_grants(proposal_id, expires_at);

CREATE TABLE IF NOT EXISTS canon_commits (
    commit_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    artifact_stage TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    chapter_no INTEGER,
    scene_index INTEGER,
    artifact_revision INTEGER NOT NULL,
    head_revision_before INTEGER NOT NULL,
    head_revision_after INTEGER NOT NULL,
    active_revision_before INTEGER NOT NULL,
    active_revision_after INTEGER NOT NULL,
    changes_authority INTEGER NOT NULL,
    accepted_request_hash TEXT NOT NULL UNIQUE,
    grant_token_hash TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    projection_hash TEXT NOT NULL DEFAULT '',
    acceptance_source_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(proposal_id) REFERENCES proposals(proposal_id),
    FOREIGN KEY(grant_token_hash) REFERENCES approval_grants(token_hash)
);

CREATE INDEX IF NOT EXISTS idx_commits_revision
    ON canon_commits(head_revision_after, operation, branch_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_accept_per_proposal
    ON canon_commits(proposal_id)
    WHERE operation = 'accept';

CREATE TABLE IF NOT EXISTS continuity_events (
    event_id TEXT PRIMARY KEY,
    commit_id TEXT NOT NULL,
    event_ordinal INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    artifact_revision INTEGER NOT NULL,
    chapter_no INTEGER,
    scene_index INTEGER,
    story_time TEXT,
    narrative_mode TEXT NOT NULL DEFAULT 'linear',
    entity_id TEXT,
    subject_entity_id TEXT,
    target_entity_id TEXT,
    payload_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(commit_id, event_ordinal),
    FOREIGN KEY(commit_id) REFERENCES canon_commits(commit_id)
);

CREATE INDEX IF NOT EXISTS idx_continuity_events_story
    ON continuity_events(branch_id, chapter_no, scene_index, event_ordinal);
CREATE INDEX IF NOT EXISTS idx_continuity_events_entity
    ON continuity_events(entity_id, subject_entity_id, target_entity_id);

CREATE TABLE IF NOT EXISTS event_links (
    link_id TEXT PRIMARY KEY,
    source_commit_id TEXT NOT NULL,
    source_event_id TEXT,
    target_event_id TEXT NOT NULL,
    link_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(source_commit_id) REFERENCES canon_commits(commit_id),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id),
    FOREIGN KEY(target_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_event_links_target
    ON event_links(target_event_id, link_type);

CREATE TABLE IF NOT EXISTS canon_facts (
    fact_key TEXT PRIMARY KEY,
    fact_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    entity_id TEXT,
    subject_entity_id TEXT,
    target_entity_id TEXT,
    field_name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    chapter_no INTEGER,
    scene_index INTEGER,
    story_time TEXT,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_canon_facts_entity
    ON canon_facts(entity_id, fact_type, field_name);

CREATE TABLE IF NOT EXISTS timeless_facts (
    fact_key TEXT PRIMARY KEY,
    fact_type TEXT NOT NULL,
    entity_id TEXT,
    subject_entity_id TEXT,
    target_entity_id TEXT,
    field_name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS planned_facts (
    fact_key TEXT PRIMARY KEY,
    fact_type TEXT NOT NULL,
    entity_id TEXT,
    subject_entity_id TEXT,
    target_entity_id TEXT,
    field_name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    chapter_no INTEGER,
    scene_index INTEGER,
    story_time TEXT,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS branch_facts (
    branch_fact_key TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    entity_id TEXT,
    subject_entity_id TEXT,
    target_entity_id TEXT,
    field_name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    chapter_no INTEGER,
    scene_index INTEGER,
    story_time TEXT,
    provisional INTEGER NOT NULL DEFAULT 1,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_branch_facts_lookup
    ON branch_facts(branch_id, entity_id, fact_type, field_name);

CREATE TABLE IF NOT EXISTS fact_versions (
    version_id TEXT PRIMARY KEY,
    fact_key TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    entity_id TEXT,
    subject_entity_id TEXT,
    target_entity_id TEXT,
    field_name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    valid_from_chapter INTEGER,
    valid_from_scene INTEGER,
    valid_to_chapter INTEGER,
    valid_to_scene INTEGER,
    story_time TEXT,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_fact_versions_point
    ON fact_versions(fact_key, valid_from_chapter, valid_from_scene);

CREATE TABLE IF NOT EXISTS location_state (
    actor_entity_id TEXT PRIMARY KEY,
    location_entity_id TEXT,
    transit_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    chapter_no INTEGER,
    scene_index INTEGER,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS inventory_state (
    inventory_key TEXT PRIMARY KEY,
    item_entity_id TEXT NOT NULL,
    owner_entity_id TEXT,
    quantity REAL,
    is_unique INTEGER NOT NULL DEFAULT 0,
    item_status TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_inventory_owner
    ON inventory_state(owner_entity_id, item_entity_id);

CREATE TABLE IF NOT EXISTS relation_state (
    relation_key TEXT PRIMARY KEY,
    source_entity_id TEXT NOT NULL,
    target_entity_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS ability_state (
    ability_key TEXT PRIMARY KEY,
    owner_entity_id TEXT NOT NULL,
    ability_entity_id TEXT NOT NULL,
    state_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

-- v5 keeps the legacy ability_state table as an active-ownership
-- compatibility projection.  Persistent ownership, volatile runtime and
-- immutable use history are projected separately so a use/cooldown/lose
-- event can no longer overwrite acquisition metadata.
CREATE TABLE IF NOT EXISTS actor_ability_state (
    ability_key TEXT PRIMARY KEY,
    owner_entity_id TEXT NOT NULL,
    ability_entity_id TEXT NOT NULL,
    acquired INTEGER NOT NULL,
    ownership_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_actor_ability_owner
    ON actor_ability_state(owner_entity_id, acquired, ability_entity_id);
CREATE INDEX IF NOT EXISTS idx_actor_ability_definition
    ON actor_ability_state(ability_entity_id, acquired, owner_entity_id);

CREATE TABLE IF NOT EXISTS ability_runtime_state (
    ability_key TEXT PRIMARY KEY,
    owner_entity_id TEXT NOT NULL,
    ability_entity_id TEXT NOT NULL,
    available INTEGER NOT NULL,
    runtime_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_ability_runtime_owner
    ON ability_runtime_state(owner_entity_id, available, ability_entity_id);

CREATE TABLE IF NOT EXISTS ability_use_history (
    source_event_id TEXT PRIMARY KEY,
    owner_entity_id TEXT NOT NULL,
    ability_entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    runtime_json TEXT NOT NULL DEFAULT '{}',
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    chapter_no INTEGER,
    scene_index INTEGER,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_ability_use_history_lookup
    ON ability_use_history(
        owner_entity_id, ability_entity_id, updated_order, source_event_id
    );

CREATE TABLE IF NOT EXISTS power_system_specs (
    spec_entity_id TEXT PRIMARY KEY,
    spec_status TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS progression_tracks (
    track_entity_id TEXT PRIMARY KEY,
    system_entity_id TEXT,
    track_kind TEXT NOT NULL,
    track_status TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS rank_nodes (
    rank_entity_id TEXT PRIMARY KEY,
    track_entity_id TEXT,
    rank_status TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_rank_nodes_track
    ON rank_nodes(track_entity_id, rank_entity_id);

CREATE TABLE IF NOT EXISTS rank_edges (
    edge_entity_id TEXT PRIMARY KEY,
    track_entity_id TEXT,
    from_rank_ids_json TEXT NOT NULL DEFAULT '[]',
    to_rank_entity_id TEXT,
    edge_status TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_rank_edges_track
    ON rank_edges(track_entity_id, to_rank_entity_id, edge_entity_id);

CREATE TABLE IF NOT EXISTS ability_definitions (
    ability_entity_id TEXT PRIMARY KEY,
    system_entity_id TEXT,
    definition_status TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS resource_definitions (
    resource_entity_id TEXT PRIMARY KEY,
    system_entity_id TEXT,
    definition_status TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS status_definitions (
    status_entity_id TEXT PRIMARY KEY,
    system_entity_id TEXT,
    definition_status TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS qualification_definitions (
    qualification_entity_id TEXT PRIMARY KEY,
    system_entity_id TEXT,
    definition_status TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS counter_rules (
    rule_entity_id TEXT PRIMARY KEY,
    rule_status TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS bridge_rules (
    rule_entity_id TEXT PRIMARY KEY,
    rule_status TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS conversion_rules (
    rule_entity_id TEXT PRIMARY KEY,
    rule_status TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS actor_progression_state (
    progression_key TEXT PRIMARY KEY,
    actor_entity_id TEXT NOT NULL,
    track_entity_id TEXT NOT NULL,
    rank_entity_id TEXT,
    state_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_actor_progression_lookup
    ON actor_progression_state(actor_entity_id, track_entity_id);

CREATE TABLE IF NOT EXISTS actor_resource_state (
    resource_key TEXT PRIMARY KEY,
    actor_entity_id TEXT NOT NULL,
    resource_entity_id TEXT NOT NULL,
    balance REAL NOT NULL,
    reserved REAL NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_actor_resource_lookup
    ON actor_resource_state(actor_entity_id, resource_entity_id);

CREATE TABLE IF NOT EXISTS actor_status_state (
    status_key TEXT PRIMARY KEY,
    actor_entity_id TEXT NOT NULL,
    status_entity_id TEXT NOT NULL,
    active INTEGER NOT NULL,
    stacks INTEGER NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_actor_status_lookup
    ON actor_status_state(actor_entity_id, active, status_entity_id);

CREATE TABLE IF NOT EXISTS power_bindings (
    binding_key TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL,
    actor_entity_id TEXT NOT NULL,
    source_entity_id TEXT NOT NULL,
    binding_kind TEXT NOT NULL,
    active INTEGER NOT NULL,
    ability_entity_ids_json TEXT NOT NULL DEFAULT '[]',
    state_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_power_bindings_actor
    ON power_bindings(actor_entity_id, active, binding_id);
CREATE INDEX IF NOT EXISTS idx_power_bindings_source
    ON power_bindings(source_entity_id, active, binding_id);

CREATE TABLE IF NOT EXISTS qualification_state (
    qualification_key TEXT PRIMARY KEY,
    actor_entity_id TEXT NOT NULL,
    qualification_entity_id TEXT NOT NULL,
    active INTEGER NOT NULL,
    quantity REAL NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_qualification_lookup
    ON qualification_state(
        actor_entity_id, active, qualification_entity_id
    );

CREATE TABLE IF NOT EXISTS power_observations (
    observation_key TEXT PRIMARY KEY,
    observer_entity_id TEXT NOT NULL,
    subject_entity_id TEXT,
    ability_entity_id TEXT,
    observation_action TEXT NOT NULL,
    knowledge_plane TEXT NOT NULL,
    observation_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_power_observations_observer
    ON power_observations(
        observer_entity_id, subject_entity_id, ability_entity_id, updated_order
    );

-- When a pre-v5 database contains orphaned legacy ability projections, the
-- migration records exactly what was imported.  This table is migration
-- provenance, not a mutable replay projection.
CREATE TABLE IF NOT EXISTS legacy_power_imports (
    import_key TEXT PRIMARY KEY,
    owner_entity_id TEXT NOT NULL,
    ability_entity_id TEXT NOT NULL,
    state_json TEXT NOT NULL,
    imported_event_id TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS belief_state (
    belief_key TEXT PRIMARY KEY,
    believer_entity_id TEXT NOT NULL,
    proposition_key TEXT NOT NULL,
    belief_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS open_loops (
    loop_id TEXT PRIMARY KEY,
    owner_entity_id TEXT,
    loop_type TEXT NOT NULL,
    loop_status TEXT NOT NULL,
    due_chapter INTEGER,
    due_scene INTEGER,
    payload_json TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS accepted_source_manifest (
    manifest_entry_id TEXT PRIMARY KEY,
    commit_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_role TEXT NOT NULL,
    manifest_status TEXT NOT NULL DEFAULT 'pending',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    activated_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(commit_id, source_id),
    FOREIGN KEY(commit_id) REFERENCES canon_commits(commit_id)
);

CREATE INDEX IF NOT EXISTS idx_source_manifest_active
    ON accepted_source_manifest(manifest_status, source_path, content_hash);

CREATE TABLE IF NOT EXISTS materialization_runs (
    run_id TEXT PRIMARY KEY,
    commit_id TEXT NOT NULL UNIQUE,
    target_root TEXT NOT NULL,
    run_status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    staging_path TEXT NOT NULL DEFAULT '',
    completion_receipt_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(commit_id) REFERENCES canon_commits(commit_id)
);

CREATE TABLE IF NOT EXISTS materialization_activation_claims (
    run_id TEXT PRIMARY KEY,
    owner_host TEXT NOT NULL,
    owner_pid INTEGER NOT NULL,
    owner_token TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES materialization_runs(run_id)
);

CREATE TABLE IF NOT EXISTS materialization_files (
    run_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    expected_old_hash TEXT,
    proposed_new_hash TEXT NOT NULL,
    actual_hash TEXT,
    file_status TEXT NOT NULL,
    PRIMARY KEY(run_id, relative_path),
    FOREIGN KEY(run_id) REFERENCES materialization_runs(run_id)
);

CREATE TABLE IF NOT EXISTS materialization_journal (
    journal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_no INTEGER NOT NULL,
    step_name TEXT NOT NULL,
    step_status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(run_id, step_no),
    FOREIGN KEY(run_id) REFERENCES materialization_runs(run_id)
);

CREATE TABLE IF NOT EXISTS projection_runs (
    run_id TEXT PRIMARY KEY,
    projection_name TEXT NOT NULL,
    source_head_revision INTEGER NOT NULL,
    source_active_revision INTEGER NOT NULL,
    run_status TEXT NOT NULL,
    projection_hash TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_projection_runs_revision
    ON projection_runs(source_head_revision, projection_name, created_at);

CREATE TABLE IF NOT EXISTS idempotency_records (
    namespace TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(namespace, idempotency_key)
);

CREATE VIEW IF NOT EXISTS active_facts AS
SELECT
    fact_key,
    fact_type,
    scope,
    entity_id,
    subject_entity_id,
    target_entity_id,
    field_name,
    value_json,
    source_event_id,
    chapter_no,
    scene_index,
    story_time,
    updated_order
FROM canon_facts
WHERE scope = 'current'
UNION ALL
SELECT
    fact_key,
    fact_type,
    'timeless' AS scope,
    entity_id,
    subject_entity_id,
    target_entity_id,
    field_name,
    value_json,
    source_event_id,
    NULL AS chapter_no,
    NULL AS scene_index,
    NULL AS story_time,
    updated_order
FROM timeless_facts;
"""


# Schema v6 is deliberately split from the v5 body.  Existing v5 databases
# receive only CREATE TABLE/INDEX statements and migration-owned projection
# rows; their accepted ledger, canon revisions, inventory projection and
# historical projection hashes are never rewritten.
CONTINUITY_V6_ADDITIVE_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS item_definitions (
    item_definition_id TEXT PRIMARY KEY,
    item_entity_id TEXT,
    item_status TEXT NOT NULL,
    item_kind TEXT NOT NULL,
    stack_policy TEXT NOT NULL,
    uniqueness_policy TEXT NOT NULL,
    definition_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT,
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(
        item_status IN (
            'active', 'deprecated', 'superseded',
            'legacy_unmodeled', 'legacy_self_instance'
        )
    ),
    CHECK(
        stack_policy IN (
            'non_stackable', 'homogeneous', 'lot', 'unknown'
        )
    ),
    CHECK(
        uniqueness_policy IN (
            'ordinary', 'unique_instance', 'unique_definition', 'unknown'
        )
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(item_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_item_definitions_entity
    ON item_definitions(item_entity_id)
    WHERE item_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_item_definitions_kind
    ON item_definitions(item_status, item_kind, item_definition_id);

CREATE TABLE IF NOT EXISTS item_instances (
    item_instance_id TEXT PRIMARY KEY,
    item_definition_id TEXT NOT NULL,
    item_entity_id TEXT,
    instance_status TEXT NOT NULL,
    instance_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(
        instance_status IN (
            'active', 'retired', 'destroyed', 'consumed',
            'legacy_self_instance'
        )
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(item_definition_id)
        REFERENCES item_definitions(item_definition_id),
    FOREIGN KEY(item_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_item_instances_entity
    ON item_instances(item_entity_id)
    WHERE item_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_item_instances_definition
    ON item_instances(
        item_definition_id, instance_status, item_instance_id
    );

CREATE TABLE IF NOT EXISTS item_stacks (
    stack_id TEXT PRIMARY KEY,
    item_definition_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    stack_status TEXT NOT NULL,
    batch_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(
        typeof(quantity) IN ('integer', 'real')
        AND quantity >= 0
        AND quantity <= 1.0e308
    ),
    CHECK(
        stack_status IN (
            'active', 'retired', 'depleted', 'merged'
        )
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(item_definition_id)
        REFERENCES item_definitions(item_definition_id),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_item_stacks_definition
    ON item_stacks(item_definition_id, stack_status, stack_id);

CREATE TABLE IF NOT EXISTS item_function_definitions (
    function_id TEXT PRIMARY KEY,
    item_definition_id TEXT NOT NULL,
    function_status TEXT NOT NULL,
    effect_owner TEXT NOT NULL,
    definition_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    CHECK(
        function_status IN ('active', 'deprecated', 'superseded')
    ),
    CHECK(effect_owner IN ('inline', 'ability_bridge')),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(item_definition_id)
        REFERENCES item_definitions(item_definition_id),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_item_functions_definition
    ON item_function_definitions(
        item_definition_id, function_status, function_id
    );

CREATE TABLE IF NOT EXISTS item_function_bindings (
    binding_id TEXT PRIMARY KEY,
    item_definition_id TEXT,
    item_instance_id TEXT,
    stack_id TEXT,
    function_id TEXT NOT NULL,
    binding_status TEXT NOT NULL,
    binding_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    updated_order INTEGER NOT NULL,
    CHECK(
        (item_definition_id IS NOT NULL)
        + (item_instance_id IS NOT NULL)
        + (stack_id IS NOT NULL) = 1
    ),
    CHECK(binding_status IN ('active', 'deprecated', 'superseded')),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(item_definition_id)
        REFERENCES item_definitions(item_definition_id),
    FOREIGN KEY(item_instance_id)
        REFERENCES item_instances(item_instance_id),
    FOREIGN KEY(stack_id) REFERENCES item_stacks(stack_id),
    FOREIGN KEY(function_id)
        REFERENCES item_function_definitions(function_id),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_item_function_bindings_target
    ON item_function_bindings(
        item_instance_id, stack_id, item_definition_id, binding_status
    );
CREATE INDEX IF NOT EXISTS idx_item_function_bindings_function
    ON item_function_bindings(function_id, binding_status, binding_id);

CREATE TABLE IF NOT EXISTS item_custody_state (
    custody_key TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    item_instance_id TEXT,
    stack_id TEXT,
    legal_owner_entity_id TEXT,
    custodian_entity_id TEXT,
    carrier_entity_id TEXT,
    location_entity_id TEXT,
    container_instance_id TEXT,
    access_controller_entity_id TEXT,
    custody_status TEXT NOT NULL,
    quantity REAL,
    state_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    CHECK(subject_type IN ('item_instance', 'item_stack')),
    CHECK(
        (item_instance_id IS NOT NULL) + (stack_id IS NOT NULL) = 1
    ),
    CHECK(
        (subject_type = 'item_instance'
         AND item_instance_id IS NOT NULL
         AND stack_id IS NULL
         AND subject_id = item_instance_id)
        OR
        (subject_type = 'item_stack'
         AND stack_id IS NOT NULL
         AND item_instance_id IS NULL
         AND subject_id = stack_id)
    ),
    CHECK(
        (
            subject_type = 'item_instance'
            AND typeof(quantity) IN ('integer', 'real')
            AND quantity = 1
        )
        OR
        (
            subject_type = 'item_stack'
            AND (
                quantity IS NULL
                OR (
                    typeof(quantity) IN ('integer', 'real')
                    AND quantity >= 0
                    AND quantity <= 1.0e308
                )
            )
        )
    ),
    CHECK(
        custody_status IN (
            'possessed', 'stored', 'loaned', 'seized', 'lost',
            'abandoned', 'in_transit', 'destroyed'
        )
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    UNIQUE(subject_type, subject_id),
    FOREIGN KEY(item_instance_id)
        REFERENCES item_instances(item_instance_id),
    FOREIGN KEY(stack_id) REFERENCES item_stacks(stack_id),
    FOREIGN KEY(legal_owner_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY(custodian_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY(carrier_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY(location_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY(container_instance_id)
        REFERENCES item_instances(item_instance_id),
    FOREIGN KEY(access_controller_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_item_custody_owner
    ON item_custody_state(
        legal_owner_entity_id, custodian_entity_id, carrier_entity_id
    );
CREATE INDEX IF NOT EXISTS idx_item_custody_location
    ON item_custody_state(location_entity_id, container_instance_id);

CREATE TABLE IF NOT EXISTS item_runtime_state (
    item_instance_id TEXT PRIMARY KEY,
    durability REAL,
    max_durability REAL,
    energy REAL,
    max_energy REAL,
    sealed INTEGER NOT NULL DEFAULT 0,
    damaged INTEGER NOT NULL DEFAULT 0,
    destroyed INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 0,
    equipped_by_entity_id TEXT,
    slot_key TEXT,
    bound_actor_entity_id TEXT,
    state_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    CHECK(
        durability IS NULL
        OR (
            typeof(durability) IN ('integer', 'real')
            AND durability >= 0
            AND durability <= 1.0e308
        )
    ),
    CHECK(
        max_durability IS NULL
        OR (
            typeof(max_durability) IN ('integer', 'real')
            AND max_durability >= 0
            AND max_durability <= 1.0e308
        )
    ),
    CHECK(
        energy IS NULL
        OR (
            typeof(energy) IN ('integer', 'real')
            AND energy >= 0
            AND energy <= 1.0e308
        )
    ),
    CHECK(
        max_energy IS NULL
        OR (
            typeof(max_energy) IN ('integer', 'real')
            AND max_energy >= 0
            AND max_energy <= 1.0e308
        )
    ),
    CHECK(
        durability IS NULL
        OR max_durability IS NULL
        OR durability <= max_durability
    ),
    CHECK(
        energy IS NULL
        OR max_energy IS NULL
        OR energy <= max_energy
    ),
    CHECK(typeof(sealed) = 'integer' AND sealed IN (0, 1)),
    CHECK(typeof(damaged) = 'integer' AND damaged IN (0, 1)),
    CHECK(typeof(destroyed) = 'integer' AND destroyed IN (0, 1)),
    CHECK(typeof(active) = 'integer' AND active IN (0, 1)),
    CHECK(
        (equipped_by_entity_id IS NULL AND slot_key IS NULL)
        OR
        (equipped_by_entity_id IS NOT NULL
         AND slot_key IS NOT NULL
         AND length(trim(slot_key)) > 0)
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(item_instance_id)
        REFERENCES item_instances(item_instance_id),
    FOREIGN KEY(equipped_by_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY(bound_actor_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_item_runtime_equipped
    ON item_runtime_state(
        equipped_by_entity_id, active, destroyed, item_instance_id
    );
CREATE INDEX IF NOT EXISTS idx_item_runtime_bound
    ON item_runtime_state(
        bound_actor_entity_id, active, destroyed, item_instance_id
    );

CREATE TABLE IF NOT EXISTS item_function_runtime_state (
    function_runtime_key TEXT PRIMARY KEY,
    item_instance_id TEXT NOT NULL,
    function_id TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    unlock_state TEXT NOT NULL,
    remaining_charges REAL,
    cooldown_until_json TEXT NOT NULL DEFAULT 'null',
    state_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    UNIQUE(item_instance_id, function_id),
    CHECK(typeof(enabled) = 'integer' AND enabled IN (0, 1)),
    CHECK(unlock_state IN ('locked', 'unlocked', 'suppressed')),
    CHECK(
        remaining_charges IS NULL
        OR (
            typeof(remaining_charges) IN ('integer', 'real')
            AND remaining_charges >= 0
            AND remaining_charges <= 1.0e308
        )
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(item_instance_id)
        REFERENCES item_instances(item_instance_id),
    FOREIGN KEY(function_id)
        REFERENCES item_function_definitions(function_id),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_item_function_runtime_ready
    ON item_function_runtime_state(
        item_instance_id, enabled, unlock_state, function_id
    );

CREATE TABLE IF NOT EXISTS item_use_history (
    source_event_id TEXT PRIMARY KEY,
    item_instance_id TEXT,
    stack_id TEXT,
    function_id TEXT,
    actor_entity_id TEXT NOT NULL,
    target_entity_id TEXT,
    action TEXT NOT NULL,
    delta_json TEXT NOT NULL DEFAULT '{}',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    chapter_no INTEGER,
    scene_index INTEGER,
    updated_order INTEGER NOT NULL,
    CHECK(
        (item_instance_id IS NOT NULL) + (stack_id IS NOT NULL) = 1
    ),
    CHECK(action IN ('use', 'trigger', 'consume')),
    CHECK(
        chapter_no IS NULL
        OR (typeof(chapter_no) = 'integer' AND chapter_no >= 1)
    ),
    CHECK(
        scene_index IS NULL
        OR (typeof(scene_index) = 'integer' AND scene_index >= 0)
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(item_instance_id)
        REFERENCES item_instances(item_instance_id),
    FOREIGN KEY(stack_id) REFERENCES item_stacks(stack_id),
    FOREIGN KEY(function_id)
        REFERENCES item_function_definitions(function_id),
    FOREIGN KEY(actor_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY(target_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_item_use_history_item
    ON item_use_history(
        item_instance_id, stack_id, updated_order, source_event_id
    );
CREATE INDEX IF NOT EXISTS idx_item_use_history_actor
    ON item_use_history(actor_entity_id, updated_order, source_event_id);

CREATE TABLE IF NOT EXISTS item_observations (
    observation_key TEXT PRIMARY KEY,
    observer_entity_id TEXT NOT NULL,
    item_instance_id TEXT,
    stack_id TEXT,
    function_id TEXT,
    observation_action TEXT NOT NULL,
    knowledge_plane TEXT NOT NULL,
    observation_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    CHECK(
        (item_instance_id IS NOT NULL) + (stack_id IS NOT NULL) = 1
    ),
    CHECK(
        observation_action IN (
            'observe', 'reveal', 'claim', 'misidentify', 'correct'
        )
    ),
    CHECK(
        knowledge_plane IN (
            'objective', 'actor_belief', 'public_narrative',
            'reader_disclosed', 'author_plan'
        )
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(observer_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY(item_instance_id)
        REFERENCES item_instances(item_instance_id),
    FOREIGN KEY(stack_id) REFERENCES item_stacks(stack_id),
    FOREIGN KEY(function_id)
        REFERENCES item_function_definitions(function_id),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_item_observations_observer
    ON item_observations(
        observer_entity_id, item_instance_id, stack_id, updated_order
    );

-- This metadata is intentionally separate from canon_commits.projection_hash.
-- Item projection evolution must not perturb the established continuity hash.
CREATE TABLE IF NOT EXISTS item_projection_meta (
    meta_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    source_event_id TEXT,
    updated_order INTEGER NOT NULL DEFAULT 0,
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE TABLE IF NOT EXISTS extraction_jobs (
    job_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    assistant_sha256 TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    retrieved_context_digest TEXT NOT NULL,
    prepared_canon_revision INTEGER NOT NULL,
    active_projection_hash TEXT NOT NULL,
    intent_contract_hash TEXT NOT NULL DEFAULT '',
    event_seed_manifest_hash TEXT NOT NULL DEFAULT '',
    event_experience_control_revision INTEGER NOT NULL DEFAULT 0,
    event_seed_references_json TEXT NOT NULL DEFAULT '[]',
    experience_contract_hashes_json TEXT NOT NULL DEFAULT '[]',
    artifact_context_json TEXT NOT NULL DEFAULT '{}',
    branch_id TEXT NOT NULL DEFAULT 'main',
    sequence_no INTEGER,
    extract_provider TEXT NOT NULL,
    extract_base_url TEXT NOT NULL,
    extract_model TEXT NOT NULL,
    extract_schema_hash TEXT NOT NULL,
    extract_prompt_template_hash TEXT NOT NULL,
    min_confidence REAL NOT NULL,
    generation_params_json TEXT NOT NULL DEFAULT '{}',
    job_binding_hash TEXT NOT NULL,
    job_status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    remote_status TEXT NOT NULL DEFAULT '',
    result_kind TEXT NOT NULL DEFAULT '',
    result_proposal_id TEXT,
    error TEXT NOT NULL DEFAULT '',
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    CHECK(
        typeof(prepared_canon_revision) = 'integer'
        AND prepared_canon_revision >= 0
    ),
    CHECK(
        sequence_no IS NULL
        OR (typeof(sequence_no) = 'integer' AND sequence_no >= 0)
    ),
    CHECK(
        typeof(event_experience_control_revision) = 'integer'
        AND event_experience_control_revision >= 0
    ),
    CHECK(
        typeof(min_confidence) IN ('integer', 'real')
        AND min_confidence >= 0
        AND min_confidence <= 1
    ),
    CHECK(
        job_status IN (
            'queued', 'running', 'succeeded', 'failed', 'cancelled'
        )
    ),
    CHECK(result_kind IN ('', 'proposal', 'no_delta')),
    CHECK(
        (
            job_status = 'succeeded'
            AND (
                (
                    result_kind = 'proposal'
                    AND result_proposal_id IS NOT NULL
                )
                OR (
                    result_kind = 'no_delta'
                    AND result_proposal_id IS NULL
                )
            )
        )
        OR (
            job_status <> 'succeeded'
            AND result_kind = ''
            AND result_proposal_id IS NULL
        )
    ),
    CHECK(typeof(attempt_count) = 'integer' AND attempt_count >= 0),
    UNIQUE(receipt_id, assistant_sha256),
    FOREIGN KEY(receipt_id) REFERENCES turns(receipt_id),
    FOREIGN KEY(result_proposal_id) REFERENCES proposals(proposal_id)
);

CREATE INDEX IF NOT EXISTS idx_extraction_jobs_status
    ON extraction_jobs(job_status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_extraction_jobs_barrier
    ON extraction_jobs(branch_id, sequence_no, job_status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_extraction_jobs_result_proposal
    ON extraction_jobs(result_proposal_id)
    WHERE result_proposal_id IS NOT NULL;

-- Generated prose is isolated from list/inspect metadata.  Workers can read
-- it only through the fenced extraction API, and successful/cancelled jobs
-- purge it after the durable proposal disposition has been recorded.
CREATE TABLE IF NOT EXISTS extraction_job_payloads (
    job_id TEXT PRIMARY KEY,
    assistant_text TEXT NOT NULL,
    assistant_sha256 TEXT NOT NULL,
    payload_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(typeof(payload_bytes) = 'integer' AND payload_bytes >= 0),
    FOREIGN KEY(job_id) REFERENCES extraction_jobs(job_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS extraction_barrier_resolutions (
    resolution_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    branch_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    expected_attempt_count INTEGER NOT NULL,
    action TEXT NOT NULL,
    replacement_job_id TEXT,
    target_branch_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(typeof(sequence_no) = 'integer' AND sequence_no >= 0),
    CHECK(
        typeof(expected_attempt_count) = 'integer'
        AND expected_attempt_count >= 0
    ),
    CHECK(
        action IN (
            'discard', 'rewrite', 'supersede', 'branch_switch'
        )
    ),
    CHECK(
        (
            action IN ('rewrite', 'supersede')
            AND replacement_job_id IS NOT NULL
        )
        OR (
            action = 'discard'
            AND replacement_job_id IS NULL
            AND target_branch_id = ''
        )
        OR (
            action = 'branch_switch'
            AND replacement_job_id IS NULL
            AND length(trim(target_branch_id)) > 0
        )
    ),
    FOREIGN KEY(job_id) REFERENCES extraction_jobs(job_id),
    FOREIGN KEY(replacement_job_id) REFERENCES extraction_jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_extraction_barrier_resolutions_branch_sequence
    ON extraction_barrier_resolutions(branch_id, sequence_no, created_at);

CREATE TABLE IF NOT EXISTS event_experience_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_seeds (
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

CREATE TABLE IF NOT EXISTS event_experience_arcs (
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

CREATE TABLE IF NOT EXISTS event_experience_contracts (
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
    ON event_experience_contracts(
        parent_chain_id, event_seed_id, contract_revision
    );

CREATE TABLE IF NOT EXISTS event_experience_reviews (
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
    FOREIGN KEY(contract_id)
        REFERENCES event_experience_contracts(contract_id)
);

CREATE INDEX IF NOT EXISTS idx_event_experience_reviews_contract
    ON event_experience_reviews(contract_id, review_revision);

CREATE TABLE IF NOT EXISTS event_experience_observed_reviews (
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

CREATE TABLE IF NOT EXISTS event_experience_questions (
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

CREATE TABLE IF NOT EXISTS event_experience_idempotency (
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(operation, idempotency_key)
);
"""


CONTINUITY_V6_SCHEMA_SQL = (
    CONTINUITY_V5_SCHEMA_SQL + "\n" + CONTINUITY_V6_ADDITIVE_SCHEMA_SQL
)

# v7 completes item runtime/knowledge separation and installs the independent
# Advantage projection.  ``advantages`` deliberately uses a lazy error import,
# so importing its pure DDL constant here does not create a validators/schema
# cycle.
from .advantages import ADVANTAGE_SCHEMA_SQL


CONTINUITY_V7_ADDITIVE_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS item_stack_function_runtime_state (
    function_runtime_key TEXT PRIMARY KEY,
    stack_id TEXT NOT NULL,
    function_id TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    unlock_state TEXT NOT NULL,
    remaining_charges REAL,
    cooldown_until_json TEXT NOT NULL DEFAULT 'null',
    state_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    UNIQUE(stack_id, function_id),
    CHECK(typeof(enabled) = 'integer' AND enabled IN (0, 1)),
    CHECK(unlock_state IN ('locked', 'unlocked', 'suppressed')),
    CHECK(
        remaining_charges IS NULL
        OR (
            typeof(remaining_charges) IN ('integer', 'real')
            AND remaining_charges >= 0
            AND remaining_charges <= 1.0e308
        )
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(stack_id) REFERENCES item_stacks(stack_id),
    FOREIGN KEY(function_id)
        REFERENCES item_function_definitions(function_id),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_item_stack_function_runtime_ready
    ON item_stack_function_runtime_state(
        stack_id, enabled, unlock_state, function_id
    );

CREATE TABLE IF NOT EXISTS item_knowledge_observations (
    observation_key TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    item_definition_id TEXT,
    item_instance_id TEXT,
    stack_id TEXT,
    observer_entity_id TEXT,
    function_id TEXT,
    observation_action TEXT NOT NULL,
    knowledge_plane TEXT NOT NULL,
    observation_json TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL,
    story_coordinate_json TEXT NOT NULL DEFAULT '{}',
    updated_order INTEGER NOT NULL,
    CHECK(
        (item_definition_id IS NOT NULL)
        + (item_instance_id IS NOT NULL)
        + (stack_id IS NOT NULL) = 1
    ),
    CHECK(
        (
            subject_type = 'item_definition'
            AND item_definition_id IS NOT NULL
            AND item_instance_id IS NULL
            AND stack_id IS NULL
            AND subject_id = item_definition_id
        )
        OR
        (
            subject_type = 'item_instance'
            AND item_definition_id IS NULL
            AND item_instance_id IS NOT NULL
            AND stack_id IS NULL
            AND subject_id = item_instance_id
        )
        OR
        (
            subject_type = 'item_stack'
            AND item_definition_id IS NULL
            AND item_instance_id IS NULL
            AND stack_id IS NOT NULL
            AND subject_id = stack_id
        )
    ),
    CHECK(
        knowledge_plane <> 'actor_belief'
        OR observer_entity_id IS NOT NULL
    ),
    CHECK(
        observation_action IN (
            'observe', 'reveal', 'claim', 'misidentify', 'correct'
        )
    ),
    CHECK(
        knowledge_plane IN (
            'objective', 'actor_belief', 'public_narrative',
            'reader_disclosed', 'author_plan'
        )
    ),
    CHECK(typeof(updated_order) = 'integer' AND updated_order >= 0),
    FOREIGN KEY(item_definition_id)
        REFERENCES item_definitions(item_definition_id),
    FOREIGN KEY(item_instance_id)
        REFERENCES item_instances(item_instance_id),
    FOREIGN KEY(stack_id) REFERENCES item_stacks(stack_id),
    FOREIGN KEY(observer_entity_id) REFERENCES entities(entity_id),
    FOREIGN KEY(function_id)
        REFERENCES item_function_definitions(function_id),
    FOREIGN KEY(source_event_id) REFERENCES continuity_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_item_knowledge_observations_observer
    ON item_knowledge_observations(
        observer_entity_id, knowledge_plane, updated_order, observation_key
    );
CREATE INDEX IF NOT EXISTS idx_item_knowledge_observations_subject
    ON item_knowledge_observations(
        subject_type, subject_id, updated_order, observation_key
    );
"""


CONTINUITY_V7_SCHEMA_SQL = (
    CONTINUITY_V6_SCHEMA_SQL
    + "\n"
    + CONTINUITY_V7_ADDITIVE_SCHEMA_SQL
    + "\n"
    + ADVANTAGE_SCHEMA_SQL
)


PROJECTION_TABLES = (
    "canon_facts",
    "timeless_facts",
    "planned_facts",
    "branch_facts",
    "fact_versions",
    "location_state",
    "inventory_state",
    "relation_state",
    "ability_state",
    "actor_ability_state",
    "ability_runtime_state",
    "ability_use_history",
    "power_system_specs",
    "progression_tracks",
    "rank_nodes",
    "rank_edges",
    "ability_definitions",
    "resource_definitions",
    "status_definitions",
    "qualification_definitions",
    "counter_rules",
    "bridge_rules",
    "conversion_rules",
    "actor_progression_state",
    "actor_resource_state",
    "actor_status_state",
    "power_bindings",
    "qualification_state",
    "power_observations",
    "belief_state",
    "open_loops",
)

ITEM_PROJECTION_TABLES = (
    "item_definitions",
    "item_instances",
    "item_stacks",
    "item_function_definitions",
    "item_function_bindings",
    "item_custody_state",
    "item_runtime_state",
    "item_function_runtime_state",
    "item_stack_function_runtime_state",
    "item_use_history",
    "item_observations",
    "item_knowledge_observations",
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

ADVANTAGE_CONTROL_TABLES = ("advantage_projection_meta",)

# Compatibility names for callers compiled against older module surfaces.
CONTINUITY_V4_SCHEMA_SQL = CONTINUITY_V5_SCHEMA_SQL


_TABLE_DEFINITION_RE = re.compile(
    r"(?im)^\s*CREATE\s+(?:VIRTUAL\s+)?TABLE\s+IF\s+NOT\s+EXISTS\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)"
)
LEGACY_V2_TABLES = frozenset(
    _TABLE_DEFINITION_RE.findall(LEGACY_V2_SCHEMA_SQL)
)
CONTINUITY_V5_TABLES = frozenset(
    _TABLE_DEFINITION_RE.findall(CONTINUITY_V5_SCHEMA_SQL)
)
CONTINUITY_V6_TABLES = frozenset(
    _TABLE_DEFINITION_RE.findall(CONTINUITY_V6_SCHEMA_SQL)
)
CONTINUITY_V7_TABLES = frozenset(
    _TABLE_DEFINITION_RE.findall(CONTINUITY_V7_SCHEMA_SQL)
)
ADVANTAGE_TABLES = frozenset(
    ADVANTAGE_PROJECTION_TABLES + ADVANTAGE_CONTROL_TABLES
)
STATE_DATABASE_TABLES = LEGACY_V2_TABLES | CONTINUITY_V7_TABLES
