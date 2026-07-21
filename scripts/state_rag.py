#!/usr/bin/env python3
"""Transactional story-state RAG for plot-rag-gate.

This module intentionally depends only on the Python standard library and the
sibling :mod:`plot_rag` module.  SQLite is the authoritative runtime ledger;
``state_snapshot.json`` is a derived, human-readable projection.

Public API:

* ``prepare_turn`` retrieves authoritative passages and current story state,
  then persists a pending turn receipt.
* ``commit_turn`` extracts evidence-backed deltas through an OpenAI-compatible
  chat endpoint and atomically appends events plus current-state projections.
* ``query_state`` performs local lexical/vector retrieval with optional remote
  embeddings and reranking.
* ``dump_state`` exposes the ledger and current projection.
* ``doctor`` checks config, schema, storage, and redacted remote readiness.
"""
from __future__ import annotations

import atexit
import email.utils
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

try:  # Package import, for example ``scripts.state_rag``.
    from .continuity.schema import (
        STATE_DATABASE_TABLES,
        SchemaVersionError,
        validate_schema_versions,
    )
    from .plot_rag import (
        CONTINUITY_STATE_SCHEMA_VERSION,
        PlotRagError,
        load_config,
        query_project,
    )
except ImportError:  # Direct CLI/runtime import with ``scripts`` on sys.path.
    from continuity.schema import (
        STATE_DATABASE_TABLES,
        SchemaVersionError,
        validate_schema_versions,
    )
    from plot_rag import (
        CONTINUITY_STATE_SCHEMA_VERSION,
        PlotRagError,
        load_config,
        query_project,
    )
try:  # Package import, for example ``scripts.state_rag``.
    from .sqlite_guard import (
        SQLiteComponentSchemaError,
        execute_sqlite_script_in_transaction,
        validate_sqlite_component_schema,
    )
except ImportError:  # Direct CLI/runtime import with ``scripts`` on sys.path.
    from sqlite_guard import (
        SQLiteComponentSchemaError,
        execute_sqlite_script_in_transaction,
        validate_sqlite_component_schema,
    )


__all__ = [
    "prepare_turn",
    "commit_turn",
    "query_state",
    "query_craft",
    "dump_state",
    "doctor",
    "normalize_item_extraction_candidate",
    "normalize_advantage_extraction_candidate",
    "validate_delta_v4_envelope",
    "split_delta_v4_results",
    "split_delta_v4_results_by_family",
    "adapt_item_extraction_candidate",
    "adapt_item_extraction_candidates",
    "adapt_advantage_extraction_candidate",
    "adapt_advantage_extraction_candidates",
    "ADVANTAGE_DELTA_EVENT_TYPES",
]

SCHEMA_VERSION = 2
LEGACY_STATE_TABLES = frozenset(
    {
        "state_meta",
        "turns",
        "state_events",
        "current_facts",
        "fact_vectors",
        "turn_commits",
    }
)
ALLOWED_CATEGORIES = (
    "character_state",
    "relationship",
    "location",
    "inventory",
    "story_time",
    "world_state",
    "ability",
    "progression",
    "resource",
    "status",
    "binding",
    "qualification",
    "observation",
)
ALLOWED_OPERATIONS = {"set", "delete"}
ALLOWED_SCOPES = {"current", "planned", "historical"}
DELTA_V3_SCHEMA = "plot-rag-delta/v3"
DELTA_V4_SCHEMA = "plot-rag-delta/v4"
EXTRACTION_AUTHORITATIVE_PROTOCOL = "json_object"
EXTRACTION_TOOL_SHADOW_PROTOCOL = "tool_function_arguments"
DEFAULT_EXTRACTION_TOOL_NAME = "submit_plot_rag_deltas"
_EXTRACTION_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
POWER_KNOWLEDGE_PLANES = {
    "objective",
    "actor_belief",
    "public_narrative",
    "reader_disclosed",
    "author_plan",
}
CONTINUITY_POWER_TABLES = frozenset(
    {
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
        "legacy_power_imports",
    }
)
_V3_EVENT_ALIASES = {
    "character_state": "state",
    "relationship": "relation",
    "location": "movement",
    "story_time": "time",
    "status": "status_effect",
    "binding": "power_binding",
    "observation": "power_observation",
}
_V3_EVENT_CATEGORIES = {
    "state": "character_state",
    "relation": "relationship",
    "movement": "location",
    "inventory": "inventory",
    "time": "story_time",
    "world_rule": "world_state",
    "ability": "ability",
    "progression": "progression",
    "resource": "resource",
    "status_effect": "status",
    "power_binding": "binding",
    "qualification": "qualification",
    "power_observation": "observation",
}
_CONTINUITY_FACT_CATEGORIES = {
    "relation": "relationship",
    "location": "location",
    "inventory": "inventory",
    "time": "story_time",
    "ability": "ability",
    "ability_ownership": "ability",
    "ability_runtime": "ability",
    "progression": "progression",
    "resource": "resource",
    "status_effect": "status",
    "power_binding": "binding",
    "qualification": "qualification",
    "power_observation": "observation",
}
_V3_ACTIONS = {
    "state": {"set"},
    "relation": {"set", "update", "remove"},
    "movement": {"move", "depart", "arrive", "teleport", "enter", "leave"},
    "inventory": {"acquire", "transfer", "consume", "lose", "set"},
    "time": {"set"},
    "world_rule": {"set"},
    "ability": {
        "gain",
        "set",
        "use",
        "cooldown",
        "breakthrough",
        "lose",
        "unlock",
        "upgrade",
        "charge",
        "activate",
        "deactivate",
        "refresh",
    },
    "progression": {
        "initialize",
        "advance",
        "regress",
        "branch",
        "prestige",
        "reset",
    },
    "resource": {
        "initialize",
        "gain",
        "spend",
        "reserve",
        "release",
        "recover",
        "convert",
        "set",
    },
    "status_effect": {"apply", "stack", "refresh", "remove", "expire"},
    "power_binding": {
        "bind",
        "unbind",
        "equip",
        "unequip",
        "contract",
        "summon",
        "dismiss",
        "suppress",
    },
    "qualification": {"grant", "revoke", "consume", "expire"},
    "power_observation": {"observe", "infer", "confirm", "disprove"},
}
_V3_SINGLE_ACTION_ECHO_REPAIRS = {
    "state": "set",
    "time": "set",
    "world_rule": "set",
}
ITEM_DELTA_EVENT_TYPES = frozenset(
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
ADVANTAGE_DELTA_EVENT_TYPES = frozenset(
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
ADVANTAGE_EVENT_SCHEMA = "plot-rag-advantage/v1"
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
_ADVANTAGE_V4_ACTIONS = {
    "advantage_spec": {"define", "update", "deprecate", "supersede"},
    "advantage_anchor": {"define", "update", "deprecate", "supersede"},
    "advantage_module": {
        "define",
        "update",
        "unlock",
        "enable",
        "lock",
        "suppress",
        "deprecate",
    },
    "advantage_bind": {"bind", "unbind", "release", "seal", "contest"},
    "advantage_activate": {"activate", "deactivate", "seal", "unseal"},
    "advantage_trigger": {"trigger"},
    "advantage_use": {"use"},
    "advantage_reward": {"reward"},
    "advantage_cost": {"cost"},
    "advantage_upgrade": {"upgrade"},
    "advantage_reveal": {"reveal"},
    "advantage_contract": {
        "define",
        "update",
        "activate",
        "suspend",
        "breach",
        "fulfill",
        "terminate",
        "narrative",
    },
    "advantage_correction": {"correct", "supersede", "retract"},
}
_ADVANTAGE_V4_SUBJECT_KINDS = {
    "advantage_spec": {"advantage_definition"},
    "advantage_anchor": {"advantage_anchor"},
    "advantage_module": {"advantage_module"},
    "advantage_bind": {"advantage"},
    "advantage_activate": {"advantage"},
    "advantage_trigger": {"advantage"},
    "advantage_use": {"advantage"},
    "advantage_reward": {"advantage"},
    "advantage_cost": {"advantage"},
    "advantage_upgrade": {"advantage"},
    "advantage_reveal": {"advantage_knowledge"},
    "advantage_contract": {
        "advantage_contract",
        "narrative_contract",
    },
    "advantage_correction": {"advantage_event"},
}
_ADVANTAGE_V4_OBJECT_ROLES = {
    "advantage_spec": {"supersedes_advantage"},
    "advantage_anchor": {"advantage", "anchor_ref", "owner"},
    "advantage_module": {
        "advantage",
        "anchor",
        "granted_ability",
    },
    "advantage_bind": {"anchor", "owner"},
    "advantage_activate": {"owner"},
    "advantage_trigger": {"module", "actor", "target", "caused_by"},
    "advantage_use": {"module", "actor", "target", "caused_by"},
    "advantage_reward": {"module", "actor", "target", "caused_by"},
    "advantage_cost": {"module", "actor", "target", "caused_by"},
    "advantage_upgrade": {"unlock_module"},
    "advantage_reveal": {
        "advantage",
        "module",
        "observer",
        "misread_of",
    },
    "advantage_contract": {
        "advantage",
        "actor",
        "counterparty",
    },
    "advantage_correction": {"target_event"},
}
_ADVANTAGE_V4_REPEATABLE_ROLES = {
    "advantage_module": {"anchor", "granted_ability"},
    "advantage_upgrade": {"unlock_module"},
}
_ADVANTAGE_V4_CHANGE_KEYS = {
    "advantage_spec": {
        "title",
        "profiles",
        "anchor_type",
        "acquisition_mode",
        "uniqueness",
        "promise",
        "counterplay",
        "definition",
    },
    "advantage_anchor": {
        "anchor_type",
        "binding_state",
        "transfer_rule",
        "attributes",
    },
    "advantage_module": {
        "title",
        "kind",
        "module_status",
        "stage",
        "trigger",
        "preconditions",
        "targets",
        "costs",
        "effects",
        "side_effects",
        "failure_modes",
        "counters",
    },
    "advantage_bind": set(),
    "advantage_activate": {"stage"},
    "advantage_trigger": {
        "costs",
        "rewards",
        "output",
        "effects",
        "side_effects",
        "cooldown",
        "pollution_delta",
        "exposure_delta",
        "debt_delta",
    },
    "advantage_use": {
        "costs",
        "rewards",
        "output",
        "effects",
        "side_effects",
        "cooldown",
        "pollution_delta",
        "exposure_delta",
        "debt_delta",
    },
    "advantage_reward": {
        "costs",
        "rewards",
        "output",
        "effects",
        "side_effects",
        "cooldown",
        "pollution_delta",
        "exposure_delta",
        "debt_delta",
    },
    "advantage_cost": {
        "costs",
        "rewards",
        "output",
        "effects",
        "side_effects",
        "cooldown",
        "pollution_delta",
        "exposure_delta",
        "debt_delta",
    },
    "advantage_upgrade": {"to_stage", "max_charges"},
    "advantage_reveal": {
        "claim",
        "reveal_stage",
        "status",
        "record_ledger",
    },
    "advantage_contract": {
        "contract_status",
        "terms",
        "agency",
        "trust_delta",
        "debt_delta",
        "breach_effect",
        "reading_promise",
        "reward_loop",
        "risk_loop",
        "reveal_ladder",
        "experience_binding",
    },
    "advantage_correction": {"replacement"},
}
_ITEM_V4_ACTIONS = {
    "item_spec": {"define", "deprecate", "supersede"},
    "item_instance": {"instantiate", "retire", "split", "merge"},
    "item_custody": {
        "acquire",
        "transfer_title",
        "handover",
        "loan",
        "return",
        "seize",
        "store",
        "retrieve",
        "lose",
        "recover",
        "abandon",
    },
    "item_runtime": {
        "bootstrap",
        "equip",
        "unequip",
        "bind",
        "unbind",
        "activate",
        "deactivate",
        "consume",
        "charge",
        "discharge",
        "repair",
        "damage",
        "break",
        "destroy",
        "seal",
        "unseal",
        "unlock_function",
        "suppress_function",
    },
    "item_function_runtime": {
        "bootstrap",
        "enable",
        "disable",
        "unlock",
        "lock",
        "suppress",
        "set_charges",
        "set_cooldown",
        "clear_cooldown",
    },
    "item_use": {"use", "trigger", "consume"},
    "item_observation": {
        "observe",
        "reveal",
        "claim",
        "misidentify",
        "correct",
    },
    "item_correction": {"correct", "supersede", "retract"},
}
_ITEM_V4_SUBJECT_KINDS = {
    "item_spec": {
        "item_definition",
        "function_definition",
        "function_binding",
    },
    "item_instance": {"item_instance", "item_stack"},
    "item_custody": {"item_instance", "item_stack"},
    "item_runtime": {"item_instance"},
    "item_function_runtime": {"item_instance", "item_stack"},
    "item_use": {"item_instance", "item_stack"},
    "item_observation": {
        "item_definition",
        "item_instance",
        "item_stack",
    },
    "item_correction": {"item_event"},
}
_ITEM_V4_OBJECT_ROLES = {
    "item_spec": {
        "item_definition",
        "item_instance",
        "item_stack",
        "function",
        "ability",
        "supersedes_spec",
    },
    "item_instance": {
        "item_definition",
        "item_entity",
        "source_stack",
        "target_stack",
    },
    "item_custody": {
        "actor",
        "from_legal_owner",
        "to_legal_owner",
        "from_carrier",
        "to_carrier",
        "from_custodian",
        "to_custodian",
        "from_access_controller",
        "to_access_controller",
        "from_container",
        "to_container",
        "from_location",
        "to_location",
    },
    "item_runtime": {
        "actor",
        "equipped_by",
        "bound_actor",
        "function",
        "slot",
        "target",
    },
    "item_function_runtime": {"function"},
    "item_use": {
        "actor",
        "function",
        "target",
        "location",
        "resource",
    },
    "item_observation": {
        "observer",
        "function",
        "observed_actor",
        "source",
    },
    "item_correction": {"target_event", "item"},
}
_ITEM_V4_CHANGE_KEYS = {
    "item_spec": {"definition", "reason"},
    "item_instance": {
        "quantity",
        "batch",
        "target_batch",
        "attributes",
        "instance_name",
        "serial_or_mark",
        "unique",
        "provenance",
        "reason",
    },
    "item_custody": {
        "quantity",
        "custody_status",
        "terms",
        "reason",
    },
    "item_runtime": {
        "delta",
        "slot_key",
        "durability",
        "max_durability",
        "energy",
        "max_energy",
        "sealed",
        "damaged",
        "destroyed",
        "active",
        "state",
        "reason",
    },
    "item_function_runtime": {
        "delta",
        "enabled",
        "unlock_state",
        "remaining_charges",
        "cooldown_until",
        "state",
        "reason",
    },
    "item_use": {"delta", "observed_effects", "reason"},
    "item_observation": {"observation", "reason"},
    "item_correction": {"replacement", "reason"},
}
_ITEM_V4_RUNTIME_CHANGE_KEYS_BY_ACTION = {
    "bootstrap": {
        "slot_key",
        "durability",
        "max_durability",
        "energy",
        "max_energy",
        "sealed",
        "damaged",
        "destroyed",
        "active",
        "state",
    },
    "equip": {"slot_key"},
    "unequip": set(),
    "bind": set(),
    "unbind": set(),
    "activate": set(),
    "deactivate": set(),
    "consume": set(),
    "charge": {"delta"},
    "discharge": {"delta"},
    "repair": {"delta"},
    "damage": {"delta"},
    "break": set(),
    "destroy": set(),
    "seal": set(),
    "unseal": set(),
    "unlock_function": set(),
    "suppress_function": {"reason"},
}
_ITEM_V4_FUNCTION_RUNTIME_CHANGE_KEYS_BY_ACTION = {
    "bootstrap": {
        "enabled",
        "unlock_state",
        "remaining_charges",
        "cooldown_until",
        "state",
    },
    "enable": set(),
    "disable": set(),
    "unlock": set(),
    "lock": set(),
    "suppress": {"reason"},
    "set_charges": {"delta", "remaining_charges"},
    "set_cooldown": {"cooldown_until"},
    "clear_cooldown": set(),
}
_ITEM_V4_DELTA_KEYS = {
    "quantity",
    "charges",
    "durability",
    "energy",
    "cooldown",
}
_ITEM_V4_DEFINITION_KEYS = {
    "item_definition": {
        "item_kind",
        "stack_policy",
        "uniqueness_policy",
        "capacity",
        "unit_bulk",
        "max_durability",
        "max_energy",
        "default_functions",
        "description",
        "tags",
    },
    "function_definition": {
        "activation",
        "prerequisites",
        "effect_owner",
        "inline_effects",
        "granted_abilities",
        "costs",
        "cooldown",
        "charges",
        "durability_cost",
        "side_effects",
        "description",
    },
    "function_binding": {
        "enabled",
        "conditions",
        "description",
    },
}
_ITEM_V4_FORBIDDEN_REMOTE_KEYS = frozenset(
    {
        "before",
        "after",
        "before_state",
        "after_state",
        "resulting_state",
        "computed_state",
        "derived_counters",
        "remaining_quantity",
        "remaining_charges",
        "remaining_durability",
        "remaining_energy",
        "cooldown_until",
        "current_quantity",
        "current_charges",
        "current_durability",
        "current_energy",
    }
)
_ITEM_V4_SCOPES = {"current", "planned", "historical", "timeless"}
DEFAULT_TOP_K = 8
DEFAULT_MAX_CONTEXT_CHARS = 12_000
DEFAULT_MIN_CONFIDENCE = 0.72
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_FACTS_SCANNED = 10_000
MAX_FIELD_CHARS = 160
MAX_SUBJECT_CHARS = 240
MAX_EVIDENCE_CHARS = 1_200
MAX_VALUE_JSON_CHARS = 32_000
CRAFT_CATALOG_PATH = Path(__file__).resolve().parents[1] / "knowledge" / "plot_design_methods.json"
CRAFT_TASKS = {
    "premise",
    "theme",
    "genre",
    "world",
    "character",
    "relationship",
    "structure",
    "continuation",
    "beat",
    "climax",
    "scene",
    "conflict",
    "suspense",
    "diagnosis",
}

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[a-z0-9_]+")
_UNCERTAIN_RE = re.compile(
    r"(?:可能|如果|假如|假设|倘若|若是|也许|或许|备选|建议|不妨|可以考虑|尚未决定|未定|待定)"
)
_FUTURE_RE = re.compile(
    r"(?:下一章|下章|接下来|未来|稍后|之后).{0,12}(?:会|将|准备|计划)|"
    r"(?:将会|将要|将在|会在|计划|打算|准备).{0,18}(?:前往|抵达|进入|离开|成为|获得|得到|失去|交给|使用|发生|改变)|"
    r"(?:明日|明天|翌日|次日|来日|今晚稍后).{0,18}(?:前往|抵达|到达|进入|离开|来到|停留)"
)
_HISTORICAL_RE = re.compile(
    r"(?:曾经|此前|当年|过去|原先|早年|昔日|昨日|昨天|昨夜|前日|前夜)"
)
_ALTERNATIVE_REQUEST_RE = re.compile(
    r"(?:给|提供|设计|列出|构思|比较|来).{0,10}(?:两|三|四|多|几)(?:个|种|套)?(?:方案|选项|走向|可能)|"
    r"(?:备选|候选|多个方案|几种可能|不同走向|方案对比)"
)
_ALTERNATIVE_CONTEXT_RE = re.compile(
    r"(?:方案|选项|备选|候选|路线|走向)[一二三四五六七八九十0-9A-Za-z]*\s*[：:]|"
    r"(?:第一种|第二种|第三种|另一种|其一|其二|其三)"
)
_MOVEMENT_TRANSITION_RE = re.compile(
    r"(?:从|离开).{0,80}(?:进入|抵达|到达|来到|前往|移至|移动到|走到|退到)"
)
_MOVEMENT_CURRENT_LOCATION_RE = re.compile(
    r"(?:当前|现在|此刻|如今|仍|已经)?\s*"
    r"(?:位于|身处|停留在|留在|进入|抵达|到达|来到|站在|躺在|坐在)"
)
_MOVEMENT_REPAIR_BLOCK_RE = re.compile(
    r"(?:尚未|还未|没有|并未|未曾|可能|也许|或许|如果|假如|假设|"
    r"计划|打算|准备|回忆|梦见|不再|明日|明天|翌日|次日|来日|"
    r"昨日|昨天|昨夜|前日|前夜)"
)
_MOVEMENT_OBSERVATION_RE = re.compile(
    r"(?:看见|看到|望见|目睹|听见|发现|得知|听说)"
)
_LOCATION_COVERAGE_RE = re.compile(
    r"(?:当前|现在|此刻|当下|如今|仍|已经|醒来时|醒来后)?"
    r".{0,16}(?:位于|身处|停留在|留在|抵达|到达|来到|站在|躺在|坐在)"
)
_LOCATION_SUBJECT_RE = re.compile(
    r"(?P<subject>[\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9_·]{2,32}?)"
    r"(?:当前|现在|此刻|当下|如今|仍|已经|刚刚|正)?\s*"
    r"(?:位于|身处|停留在|留在|抵达|到达|来到|站在|躺在|坐在)"
)
_TIME_COVERAGE_RE = re.compile(
    r"(?:故事时间|当前时间|此时|现在|当下|时间).{0,16}"
    r"(?:是|为|到了|来到|已到|已经|推进到|更新为)|"
    r"(?:此刻|此时|现在|当下).{0,8}"
    r"(?:正值|正是|已是|来到)?\s*"
    r"(?:清晨|黎明|拂晓|正午|黄昏|傍晚|午夜|深夜|凌晨)"
)
_COVERAGE_BLOCK_RE = re.compile(
    r"(?:尚未|还未|没有|并未|未曾|可能|也许|或许|如果|假如|假设|"
    r"计划|打算|准备|回忆|梦见|不再|明日|明天|翌日|次日|来日|"
    r"昨日|昨天|昨夜|前日|前夜)"
)
_POWER_EVENT_TYPES = {
    "ability",
    "progression",
    "resource",
    "status_effect",
    "power_binding",
    "qualification",
    "power_observation",
}


def _has_explicit_movement_route(
    *,
    subject: str,
    origin: str,
    destination: str,
    evidence: str,
) -> bool:
    if not subject or not origin or not destination or origin == destination:
        return False
    for clause in (
        value.strip()
        for value in re.split(r"[。！？!?；;\n]+", evidence)
        if value.strip()
    ):
        subject_offset = clause.find(subject)
        origin_offset = clause.find(origin, subject_offset + len(subject))
        destination_offset = clause.find(
            destination,
            origin_offset + len(origin),
        )
        if not (
            0 <= subject_offset < origin_offset < destination_offset
            and _MOVEMENT_TRANSITION_RE.search(clause)
        ):
            continue
        actor_span = clause[
            subject_offset + len(subject) : destination_offset
        ]
        if not _MOVEMENT_OBSERVATION_RE.search(actor_span):
            return True
    return False


def _normalize_movement_set_action(
    *,
    subject: str,
    destination: str,
    value: Any,
    evidence: str,
    scope: str,
    knowledge_plane: str,
) -> str | None:
    """Repair only a verbatim, objective current-location model slip."""

    if scope != "current" or knowledge_plane != "objective":
        return None
    origin = (
        str(value.get("from_location") or "").strip()
        if isinstance(value, dict)
        else ""
    )
    clauses = [
        clause.strip()
        for clause in re.split(r"[。！？!?；;\n]+", evidence)
        if clause.strip()
    ]
    for clause in clauses:
        if (
            subject not in clause
            or destination not in clause
            or _MOVEMENT_REPAIR_BLOCK_RE.search(clause)
        ):
            continue
        subject_offset = clause.find(subject)
        destination_offset = clause.find(destination)
        if destination_offset <= subject_offset:
            continue
        actor_span = clause[
            subject_offset + len(subject) : destination_offset
        ]
        if _MOVEMENT_OBSERVATION_RE.search(actor_span):
            continue
        if origin:
            if _has_explicit_movement_route(
                subject=subject,
                origin=origin,
                destination=destination,
                evidence=clause,
            ):
                return "move"
            continue
        location_match = _MOVEMENT_CURRENT_LOCATION_RE.search(
            clause,
            subject_offset + len(subject),
            destination_offset,
        )
        if location_match is not None:
            return "arrive"
    return None
_CRAFT_TASK_PATTERNS = {
    "premise": re.compile(r"(?:立项|创意|命题|一句话故事|故事承诺|开局定位)"),
    "theme": re.compile(r"(?:主题|立意|价值|主导意念|理念冲突)"),
    "genre": re.compile(r"(?:类型|题材|套路|反套路|爽点|读者体验|类型承诺)"),
    "world": re.compile(r"(?:世界观|设定|规则|制度|力量体系|社会结构|资源限制)"),
    "character": re.compile(r"(?:主角|人物|角色|人物弧|成长|动机|缺陷|主动性)"),
    "relationship": re.compile(r"(?:关系|盟友|对手|反派|导师|群像|配角)"),
    "structure": re.compile(r"(?:全书|卷纲|大纲|主线|支线|事件链|结构|闭环|长篇)"),
    "continuation": re.compile(r"(?:继续|推进|后续|接下来|下一章|下章|下一幕|下一场|然后)"),
    "beat": re.compile(r"(?:节拍|情节点|触发|转折|中点|假胜利|假失败|余波)"),
    "climax": re.compile(r"(?:危机|高潮|结局|终局|两难|收束|最终选择)"),
    "scene": re.compile(r"(?:场景|章节|正文|对话|分场|序列|离场|场面)"),
    "conflict": re.compile(r"(?:冲突|阻力|困境|升级|压迫|代价|对立力量)"),
    "suspense": re.compile(r"(?:伏笔|悬念|秘密|信息差|铺垫|回收|误导|揭露)"),
    "diagnosis": re.compile(r"(?:修复|重写|疲软|失效|问题|诊断|不合理|拖沓|重复)"),
}
_BUILTIN_TRUSTED_HOSTS = {
    "embedding": {"api-inference.modelscope.cn", "api.siliconflow.cn"},
    "rerank": {"api.jina.ai", "api.siliconflow.cn"},
    "extract": {"api.siliconflow.cn"},
}
_SHARED_CREDENTIAL_HOSTS = {
    "SILICONFLOW_API_KEY": {"api.siliconflow.cn"},
}
_REMOTE_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_REMOTE_MAX_CONNECTIONS_PER_SERVICE = 4
_REMOTE_USER_AGENT = "plot-rag-gate/1.6.5 state-rag"
_REMOTE_RETRYABLE_HTTP_STATUSES = frozenset({429, 503})
_REMOTE_MAX_ATTEMPTS = 3
_REMOTE_RETRY_BASE_SECONDS = 0.25
_REMOTE_RETRY_MAX_BACKOFF_SECONDS = 2.0
_REMOTE_RETRY_AFTER_MAX_SECONDS = 30.0


class StateRagError(RuntimeError):
    """A state-RAG failure that must not be interpreted as a missing fact."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep credentials on the originally validated remote endpoint only."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        try:
            fp.close()
        finally:
            raise StateRagError("remote redirects are blocked")


_RemoteConnection = http.client.HTTPConnection | http.client.HTTPSConnection
_RemotePoolKey = tuple[str, str, str, int]


class _RemoteConnectionPool:
    """Bounded, thread-safe keep-alive connections partitioned by service."""

    def __init__(self, max_connections_per_service: int) -> None:
        self._max_connections_per_service = max_connections_per_service
        self._lock = threading.Lock()
        self._idle: dict[_RemotePoolKey, list[_RemoteConnection]] = {}
        self._limits: dict[_RemotePoolKey, threading.BoundedSemaphore] = {}

    def _limit_for(self, key: _RemotePoolKey) -> threading.BoundedSemaphore:
        with self._lock:
            limit = self._limits.get(key)
            if limit is None:
                limit = threading.BoundedSemaphore(
                    self._max_connections_per_service
                )
                self._limits[key] = limit
            return limit

    @staticmethod
    def _new_connection(
        key: _RemotePoolKey, timeout_seconds: float
    ) -> _RemoteConnection:
        _service_name, scheme, host, port = key
        connection_type: type[_RemoteConnection]
        if scheme == "https":
            connection_type = http.client.HTTPSConnection
        else:
            connection_type = http.client.HTTPConnection
        return connection_type(host, port=port, timeout=timeout_seconds)

    @staticmethod
    def _set_timeout(
        connection: _RemoteConnection, timeout_seconds: float
    ) -> None:
        connection.timeout = timeout_seconds
        if connection.sock is not None:
            connection.sock.settimeout(timeout_seconds)

    def acquire(
        self, key: _RemotePoolKey, timeout_seconds: float
    ) -> _RemoteConnection:
        limit = self._limit_for(key)
        if not limit.acquire(timeout=timeout_seconds):
            raise TimeoutError("timed out waiting for an available remote connection")
        try:
            connection: _RemoteConnection | None = None
            with self._lock:
                idle = self._idle.get(key)
                while idle:
                    candidate = idle.pop()
                    if candidate.sock is None:
                        candidate.close()
                        continue
                    connection = candidate
                    break
                if idle == []:
                    self._idle.pop(key, None)
            if connection is None:
                connection = self._new_connection(key, timeout_seconds)
            self._set_timeout(connection, timeout_seconds)
            return connection
        except BaseException:
            limit.release()
            raise

    def release(
        self,
        key: _RemotePoolKey,
        connection: _RemoteConnection,
        *,
        reusable: bool,
    ) -> None:
        limit = self._limit_for(key)
        try:
            retained = False
            if reusable and connection.sock is not None:
                with self._lock:
                    idle = self._idle.setdefault(key, [])
                    if len(idle) < self._max_connections_per_service:
                        idle.append(connection)
                        retained = True
            if not retained:
                connection.close()
        finally:
            limit.release()

    def close_all(self) -> None:
        """Close idle sockets, primarily for deterministic process/test teardown."""

        with self._lock:
            connections = [
                connection
                for idle in self._idle.values()
                for connection in idle
            ]
            self._idle.clear()
        for connection in connections:
            with suppress(Exception):
                connection.close()


@dataclass(frozen=True)
class _RemoteRetryLease:
    token: object
    generation: int


@dataclass
class _RemoteRetryState:
    generation: int = 0
    cooldown_until: float = 0.0
    owner_token: object | None = None
    waiters: int = 0


class _RemoteRetryCoordinator:
    """Serialize throttled probes while leaving ordinary traffic concurrent."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.Lock())
        self._states: dict[_RemotePoolKey, _RemoteRetryState] = {}

    @staticmethod
    def _expired(state: _RemoteRetryState, now: float) -> bool:
        return (
            state.owner_token is None
            and state.waiters == 0
            and state.cooldown_until <= now
        )

    def acquire(
        self,
        key: _RemotePoolKey,
        *,
        deadline: float,
        required: bool,
    ) -> _RemoteRetryLease | None:
        """Wait for a single retry probe when this origin is throttled."""

        with self._condition:
            now = time.monotonic()
            state = self._states.get(key)
            if state is not None and self._expired(state, now):
                self._states.pop(key, None)
                state = None
            if state is None:
                if not required:
                    return None
                state = _RemoteRetryState()
                self._states[key] = state

            state.waiters += 1
            try:
                while True:
                    now = time.monotonic()
                    remaining = deadline - now
                    if remaining <= 0:
                        raise TimeoutError(
                            "timed out waiting for the remote retry window"
                        )
                    if (
                        state.owner_token is None
                        and state.cooldown_until <= now
                    ):
                        token = object()
                        state.owner_token = token
                        return _RemoteRetryLease(
                            token=token,
                            generation=state.generation,
                        )
                    wait_seconds = remaining
                    if state.cooldown_until > now:
                        wait_seconds = min(
                            wait_seconds,
                            state.cooldown_until - now,
                        )
                    self._condition.wait(timeout=max(0.001, wait_seconds))
            finally:
                state.waiters -= 1
                if self._expired(state, time.monotonic()):
                    self._states.pop(key, None)

    def record_retryable(
        self,
        key: _RemotePoolKey,
        *,
        lease: _RemoteRetryLease | None,
        delay_seconds: float,
    ) -> None:
        """Publish a shared cooldown without disturbing another live probe."""

        with self._condition:
            state = self._states.get(key)
            if state is None:
                state = _RemoteRetryState()
                self._states[key] = state
            state.generation += 1
            state.cooldown_until = max(
                state.cooldown_until,
                time.monotonic() + max(0.0, delay_seconds),
            )
            if (
                lease is not None
                and state.owner_token is lease.token
            ):
                state.owner_token = None
            self._condition.notify_all()

    def complete(
        self,
        key: _RemotePoolKey,
        lease: _RemoteRetryLease | None,
    ) -> None:
        """Release one coordinated probe and wake exactly the shared queue."""

        if lease is None:
            return
        with self._condition:
            state = self._states.get(key)
            if state is None or state.owner_token is not lease.token:
                return
            state.owner_token = None
            if state.generation == lease.generation:
                state.cooldown_until = 0.0
            if self._expired(state, time.monotonic()):
                self._states.pop(key, None)
            self._condition.notify_all()

    def clear_all(self) -> None:
        """Reset transient throttle state for deterministic teardown."""

        with self._condition:
            self._states.clear()
            self._condition.notify_all()


_REMOTE_CONNECTION_POOL = _RemoteConnectionPool(
    _REMOTE_MAX_CONNECTIONS_PER_SERVICE
)
_REMOTE_RETRY_COORDINATOR = _RemoteRetryCoordinator()
atexit.register(_REMOTE_CONNECTION_POOL.close_all)
atexit.register(_REMOTE_RETRY_COORDINATOR.clear_all)


class _ClosingConnection(sqlite3.Connection):
    """Make ``with connection`` close the Windows file handle on exit."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    enabled: bool
    base_url: str
    model: str
    api_key_env: str
    api_key_required: bool
    endpoint: str
    timeout_seconds: float
    max_tokens: int = 2_400


@dataclass(frozen=True)
class CraftConfig:
    enabled: bool
    auto_retrieve: bool
    use_embedding: bool
    use_rerank: bool
    top_k: int
    candidate_pool: int
    max_context_chars: int


@dataclass(frozen=True)
class ExtractionProtocolConfig:
    authoritative_protocol: str = EXTRACTION_AUTHORITATIVE_PROTOCOL
    tool_schema_shadow: bool = False
    tool_name: str = DEFAULT_EXTRACTION_TOOL_NAME


@dataclass(frozen=True)
class RuntimeConfig:
    root: Path
    version: int
    enabled: bool
    db_path: Path
    snapshot_path: Path
    commit_dir: Path
    auto_retrieve: bool
    auto_record: bool
    categories: tuple[str, ...]
    top_k: int
    max_context_chars: int
    min_confidence: float
    craft: CraftConfig
    timeout_seconds: float
    embedding: ServiceConfig
    rerank: ServiceConfig
    extract: ServiceConfig
    extraction_protocol: ExtractionProtocolConfig = ExtractionProtocolConfig()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_dumps(value).encode("utf-8")).hexdigest()


def _extraction_tool_schema(version: int) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "deltas": {
            "type": "array",
            "maxItems": 500,
            "items": {"type": "object"},
        }
    }
    required = ["deltas"]
    if version >= 3:
        properties["schema_version"] = {
            "type": "string",
            "enum": [DELTA_V4_SCHEMA],
        }
        required.insert(0, "schema_version")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _extraction_protocol_identity(
    config: RuntimeConfig,
) -> dict[str, Any]:
    tool_schema = _extraction_tool_schema(config.version)
    protocol = config.extraction_protocol
    return {
        "authoritative_protocol": EXTRACTION_AUTHORITATIVE_PROTOCOL,
        "authoritative_response_format_hash": _sha256_json(
            {"type": "json_object"}
        ),
        "tool_shadow": {
            "enabled": bool(protocol.tool_schema_shadow),
            "protocol": EXTRACTION_TOOL_SHADOW_PROTOCOL,
            "tool_name": protocol.tool_name,
            "schema_hash": _sha256_json(tool_schema),
            "acceptance_eligible": False,
        },
    }


def _extraction_generation_params(
    config: RuntimeConfig,
) -> dict[str, Any]:
    """Return the complete immutable model/protocol generation identity."""

    return {
        "temperature": 0,
        "max_tokens": int(config.extract.max_tokens),
        **_extraction_protocol_identity(config),
    }


def _hash(*parts: Any, length: int = 32) -> str:
    payload = "\x1f".join(str(part or "") for part in parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:length]


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _bounded_int(value: Any, default: int, minimum: int, maximum: int, name: str) -> int:
    if value is None:
        return default
    if type(value) is not int:
        raise StateRagError(f"config.{name} must be an integer")
    number = value
    if not minimum <= number <= maximum:
        raise StateRagError(f"config.{name} must be between {minimum} and {maximum}")
    return number


def _bounded_float(
    value: Any, default: float, minimum: float, maximum: float, name: str
) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateRagError(f"config.{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise StateRagError(f"config.{name} must be between {minimum} and {maximum}")
    return number


def _resolve_env_name(value: Any, default: str, field: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise StateRagError(f"config.remote.{field} must be a non-empty environment variable name")
    name = value.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise StateRagError(f"config.remote.{field} is not a valid environment variable name")
    return name


def _loaded_service_config(
    name: str,
    raw: dict[str, Any],
    *,
    timeout_seconds: float,
    default_endpoint: str,
) -> ServiceConfig:
    base_env = _resolve_env_name(raw.get("base_url_env"), "", f"{name}.base_url_env")
    model_env = _resolve_env_name(raw.get("model_env"), "", f"{name}.model_env")
    key_env = _resolve_env_name(raw.get("api_key_env"), "", f"{name}.api_key_env")
    base_url = str(os.environ.get(base_env, "")).strip() or str(raw.get("base_url", "")).strip()
    model = str(os.environ.get(model_env, "")).strip() or str(raw.get("model", "")).strip()
    enabled = bool(raw["enabled"])
    endpoint = raw.get("endpoint", default_endpoint)
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise StateRagError(f"config.remote.{name}.endpoint must be a non-empty string")
    api_key_required = bool(raw["api_key_required"])
    service_timeout = _bounded_float(
        raw.get("timeout_seconds"), timeout_seconds, 0.2, 300.0, f"remote.{name}.timeout_seconds"
    )
    max_tokens = _bounded_int(
        raw.get("max_tokens"), 2_400, 128, 32_000, f"remote.{name}.max_tokens"
    )
    return ServiceConfig(
        name=name,
        enabled=enabled,
        base_url=base_url,
        model=model,
        api_key_env=key_env,
        api_key_required=api_key_required,
        endpoint=endpoint.strip(),
        timeout_seconds=service_timeout,
        max_tokens=max_tokens,
    )


def _load_extraction_protocol_config(
    root: Path,
) -> ExtractionProtocolConfig:
    path = root / ".plot-rag" / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateRagError(
            f"cannot load extraction protocol config: {exc}"
        ) from exc
    section = raw.get("extraction_protocol", {}) if isinstance(raw, dict) else {}
    if not isinstance(section, dict):
        raise StateRagError("config.extraction_protocol must be an object")
    supported = {
        "authoritative_protocol",
        "tool_schema_shadow",
        "tool_name",
    }
    unknown = sorted(set(section) - supported)
    if unknown:
        raise StateRagError(
            "config.extraction_protocol contains unsupported fields: "
            + ", ".join(unknown)
        )
    authoritative = section.get(
        "authoritative_protocol",
        EXTRACTION_AUTHORITATIVE_PROTOCOL,
    )
    if authoritative != EXTRACTION_AUTHORITATIVE_PROTOCOL:
        raise StateRagError(
            "config.extraction_protocol.authoritative_protocol must remain "
            "json_object"
        )
    shadow = section.get("tool_schema_shadow", False)
    if not isinstance(shadow, bool):
        raise StateRagError(
            "config.extraction_protocol.tool_schema_shadow must be a boolean"
        )
    tool_name = section.get("tool_name", DEFAULT_EXTRACTION_TOOL_NAME)
    if (
        not isinstance(tool_name, str)
        or _EXTRACTION_TOOL_NAME_RE.fullmatch(tool_name.strip()) is None
    ):
        raise StateRagError(
            "config.extraction_protocol.tool_name must be a portable "
            "function name"
        )
    return ExtractionProtocolConfig(
        authoritative_protocol=EXTRACTION_AUTHORITATIVE_PROTOCOL,
        tool_schema_shadow=shadow,
        tool_name=tool_name.strip(),
    )


def _load_runtime_config(project_root: Path | str) -> RuntimeConfig:
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise StateRagError(f"project root does not exist or is not a directory: {root}")
    try:
        loaded = load_config(root)
    except PlotRagError as exc:
        raise StateRagError(str(exc)) from exc
    state = loaded["state"]
    remote = loaded["remote"]
    timeout = float(remote["timeout_seconds"])
    embedding = _loaded_service_config(
        "embedding",
        remote["embedding"],
        timeout_seconds=timeout,
        default_endpoint="embeddings",
    )
    rerank = _loaded_service_config(
        "rerank",
        remote["rerank"],
        timeout_seconds=timeout,
        default_endpoint="rerank",
    )
    extract = _loaded_service_config(
        "extract",
        remote["extract"],
        timeout_seconds=timeout,
        default_endpoint="chat/completions",
    )
    craft_raw = loaded["craft"]
    craft = CraftConfig(
        enabled=bool(craft_raw["enabled"]),
        auto_retrieve=bool(craft_raw["auto_retrieve"]),
        use_embedding=bool(craft_raw["use_embedding"]),
        use_rerank=bool(craft_raw["use_rerank"]),
        top_k=int(craft_raw["top_k"]),
        candidate_pool=int(craft_raw["candidate_pool"]),
        max_context_chars=int(craft_raw["max_context_chars"]),
    )
    extraction_protocol = _load_extraction_protocol_config(root)
    return RuntimeConfig(
        root=root,
        version=int(loaded["version"]),
        enabled=bool(loaded["enabled"] and state["enabled"]),
        db_path=Path(state["db_path"]),
        snapshot_path=Path(state["snapshot_path"]),
        commit_dir=Path(state["commit_dir"]),
        auto_retrieve=bool(state["auto_retrieve"]),
        auto_record=bool(state["auto_record"]),
        categories=tuple(state["categories"]),
        top_k=int(state["top_k"]),
        max_context_chars=int(state["max_context_chars"]),
        min_confidence=float(state["min_confidence"]),
        craft=craft,
        timeout_seconds=timeout,
        embedding=embedding,
        rerank=rerank,
        extract=extract,
        extraction_protocol=extraction_protocol,
    )


def _initialize_database_after_validation(
    connection: sqlite3.Connection,
) -> None:
    execute_sqlite_script_in_transaction(
        connection,
        """
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
        """,
    )
    event_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(state_events)")
    }
    if "scope" not in event_columns:
        connection.execute(
            "ALTER TABLE state_events ADD COLUMN scope TEXT NOT NULL DEFAULT 'current'"
        )
    if "effective_at" not in event_columns:
        connection.execute("ALTER TABLE state_events ADD COLUMN effective_at TEXT")
    fact_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(current_facts)")
    }
    if "effective_at" not in fact_columns:
        connection.execute("ALTER TABLE current_facts ADD COLUMN effective_at TEXT")
    turn_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(turns)")
    }
    if "craft_json" not in turn_columns:
        connection.execute(
            "ALTER TABLE turns ADD COLUMN craft_json TEXT NOT NULL DEFAULT '{}'"
        )
    commit_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(turn_commits)")
    }
    if "craft_json" not in commit_columns:
        connection.execute(
            "ALTER TABLE turn_commits ADD COLUMN craft_json TEXT NOT NULL DEFAULT '{}'"
        )
    connection.execute(
        """
        INSERT INTO state_meta(key, value, updated_at) VALUES('schema_version', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (str(SCHEMA_VERSION), _utc_now()),
    )
    connection.commit()
    connection.execute("PRAGMA journal_mode = WAL")


def _open_database(config: RuntimeConfig) -> sqlite3.Connection:
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(config.db_path), timeout=15.0, factory=_ClosingConnection
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("BEGIN IMMEDIATE")
        try:
            tables = validate_sqlite_component_schema(
                connection,
                component="legacy continuity state",
                meta_table="state_meta",
                version_key="schema_version",
                supported_version=SCHEMA_VERSION,
                owned_tables=LEGACY_STATE_TABLES,
                allowed_tables=STATE_DATABASE_TABLES,
            )
            if "state_meta" in tables:
                legacy_row = connection.execute(
                    "SELECT value FROM state_meta WHERE key='schema_version'"
                ).fetchone()
                continuity_row = connection.execute(
                    "SELECT value FROM state_meta "
                    "WHERE key='continuity_schema_version'"
                ).fetchone()
                legacy_version = (
                    int(legacy_row[0]) if legacy_row is not None else 0
                )
                continuity_version = (
                    int(continuity_row[0])
                    if continuity_row is not None
                    else 0
                )
            else:
                legacy_version = 0
                continuity_version = 0
            validate_schema_versions(
                user_tables_present=bool(tables),
                legacy_version=legacy_version,
                continuity_version=continuity_version,
            )
        except (
            SQLiteComponentSchemaError,
            SchemaVersionError,
            TypeError,
            ValueError,
            sqlite3.DatabaseError,
        ) as exc:
            raise StateRagError(str(exc)) from exc
        _initialize_database_after_validation(connection)
    except BaseException:
        with suppress(sqlite3.Error):
            if connection.in_transaction:
                connection.rollback()
        with suppress(sqlite3.Error):
            connection.close()
        raise
    return connection


def _open_readonly_database(config: RuntimeConfig) -> sqlite3.Connection:
    if not config.db_path.is_file():
        raise FileNotFoundError(str(config.db_path))
    uri = config.db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=15.0,
        factory=_ClosingConnection,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection
    except BaseException:
        with suppress(sqlite3.Error):
            connection.close()
        raise


def _storage_signature(path: Path) -> tuple[bool, int, int, str]:
    """Return an exact source signature without opening the file through SQLite."""

    if not path.is_file():
        return False, 0, 0, ""
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return True, int(stat.st_size), int(stat.st_mtime_ns), digest.hexdigest()


@contextmanager
def _open_diagnostic_database(config: RuntimeConfig) -> Iterator[sqlite3.Connection]:
    """Open a private byte snapshot so diagnostics never touch source WAL/SHM.

    A normal SQLite ``mode=ro`` connection may still create or mutate WAL
    sidecars.  Diagnostics therefore copy the main database and WAL into a
    temporary directory, verify that the source stayed stable during the copy,
    and query only the private snapshot.  The source SHM is deliberately not
    copied so SQLite can rebuild disposable read marks beside the snapshot.
    """

    source = config.db_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(str(source))
    source_paths = (
        source,
        Path(str(source) + "-wal"),
        Path(str(source) + "-shm"),
    )
    last_error = "database changed while creating a read-only diagnostic snapshot"
    for _attempt in range(3):
        before = tuple(_storage_signature(path) for path in source_paths)
        if not before[0][0]:
            raise FileNotFoundError(str(source))
        with tempfile.TemporaryDirectory(prefix="plot-rag-diagnostic-") as temporary:
            snapshot = Path(temporary) / source.name
            try:
                shutil.copyfile(source, snapshot)
                if before[1][0]:
                    shutil.copyfile(source_paths[1], Path(str(snapshot) + "-wal"))
            except OSError:
                last_error = (
                    "database files changed while creating a read-only diagnostic "
                    "snapshot; retry when the writer is idle"
                )
                continue
            after = tuple(_storage_signature(path) for path in source_paths)
            if before != after:
                last_error = (
                    "database changed while creating a read-only diagnostic snapshot; "
                    "retry when the writer is idle"
                )
                continue
            uri = snapshot.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=15.0,
                factory=_ClosingConnection,
            )
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA busy_timeout = 15000")
                yield connection
            finally:
                connection.close()
            return
    raise StateRagError(last_error)


def _default_remote_status(config: RuntimeConfig | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("embedding", "rerank", "extract"):
        service = getattr(config, name) if config is not None else None
        result[name] = _service_readiness(service)
    result["status"] = _remote_overall(result)
    return result


def _service_readiness(service: ServiceConfig | None) -> dict[str, Any]:
    if service is None:
        return {"status": "unknown", "configured": False}
    key_present = bool(os.environ.get(service.api_key_env, ""))
    host = _url_host(service.base_url)
    trusted = _service_host_trusted(service)
    policy_error = _service_url_policy_error(service)
    public = {
        "status": "not_called",
        "configured": False,
        "enabled": service.enabled,
        "model": service.model,
        "api_key_env": service.api_key_env,
        "api_key_present": key_present,
        "local_no_key_allowed": _is_local_url(service.base_url),
        "host": host,
        "host_trusted": trusted,
        "url_policy_ok": policy_error is None,
    }
    if not service.enabled:
        public["status"] = "disabled"
        return public
    if not service.base_url or not service.model:
        public["status"] = "unconfigured"
        public["reason"] = "base_url_or_model_missing"
        return public
    if not trusted:
        public["status"] = "unconfigured"
        public["reason"] = (
            f"host {host or '<invalid>'} is not trusted; add it to PLOT_RAG_TRUSTED_HOSTS"
        )
        return public
    if policy_error is not None:
        public["status"] = "unconfigured"
        public["reason"] = policy_error
        return public
    if service.api_key_required and not key_present and not _is_local_url(service.base_url):
        public["status"] = "unconfigured"
        public["reason"] = f"environment variable {service.api_key_env} is empty"
        return public
    public["configured"] = True
    return public


def _remote_overall(remote: dict[str, Any]) -> str:
    states = [
        value.get("status")
        for key, value in remote.items()
        if key in {"embedding", "rerank", "extract"} and isinstance(value, dict)
    ]
    if any(value == "failed" for value in states):
        return "degraded"
    if any(value == "ok" for value in states):
        return "ok"
    if any(value == "not_called" for value in states):
        return "ok"
    if any(value in {"unconfigured", "unknown"} for value in states):
        return "degraded"
    return "disabled"


def _is_local_url(value: str) -> bool:
    try:
        host = (urllib.parse.urlparse(value).hostname or "").lower()
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")


def _url_host(value: str) -> str:
    try:
        return (urllib.parse.urlparse(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _service_url_policy_error(service: ServiceConfig) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(service.base_url)
    except ValueError:
        return "base_url is invalid"
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        return "base_url must be an http(s) origin/path without credentials, query, or fragment"
    if parsed.scheme != "https" and not _is_local_url(service.base_url):
        return "HTTPS is required for non-loopback remote services"
    allowed_hosts = _SHARED_CREDENTIAL_HOSTS.get(service.api_key_env)
    if allowed_hosts is not None and host not in allowed_hosts:
        return (
            f"environment variable {service.api_key_env} is restricted to "
            f"{', '.join(sorted(allowed_hosts))}; use a service-specific key "
            f"environment variable for host {host}"
        )
    return None


def _service_host_trusted(service: ServiceConfig) -> bool:
    host = _url_host(service.base_url)
    if not host:
        return False
    if host in _BUILTIN_TRUSTED_HOSTS.get(service.name, set()):
        return True
    configured = {
        item.strip().lower()
        for item in re.split(r"[,;\s]+", os.environ.get("PLOT_RAG_TRUSTED_HOSTS", ""))
        if item.strip()
    }
    return host in configured


def _service_url(service: ServiceConfig) -> str:
    base = service.base_url.strip().rstrip("/")
    endpoint = service.endpoint.strip().lstrip("/")
    if not base:
        raise StateRagError(f"remote {service.name} base_url is not configured")
    base_path = urllib.parse.urlparse(base).path.rstrip("/").lower()
    endpoint_path = "/" + endpoint.lower().rstrip("/")
    if base_path.endswith(endpoint_path):
        return base
    return f"{base}/{endpoint}"


def _remote_connection_target(
    service: ServiceConfig,
) -> tuple[_RemotePoolKey, str]:
    url = _service_url(service)
    try:
        parsed = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(service.base_url)
        port = parsed.port
        base_port = base.port
    except ValueError as exc:
        raise StateRagError(f"remote {service.name} URL is invalid") from exc
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    base_scheme = base.scheme.lower()
    base_host = (base.hostname or "").lower().rstrip(".")
    effective_port = port or (443 if scheme == "https" else 80)
    effective_base_port = base_port or (443 if base_scheme == "https" else 80)
    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.fragment)
        or scheme != base_scheme
        or host != base_host
        or effective_port != effective_base_port
    ):
        raise StateRagError(
            f"remote {service.name} endpoint must remain on its validated origin"
        )
    target = urllib.parse.urlunsplit(
        ("", "", parsed.path or "/", parsed.query, "")
    )
    return (service.name, scheme, host, effective_port), target


def _remote_retry_delay_seconds(
    headers: Mapping[str, Any],
    *,
    retry_index: int,
    now_epoch_seconds: float | None = None,
) -> float:
    """Return a bounded Retry-After delay with a deterministic fallback.

    ``Retry-After`` accepts either a non-negative delay in seconds or an
    HTTP-date.  Malformed, negative, or otherwise unusable values deliberately
    fall back to the short exponential schedule so an upstream cannot hold a
    worker indefinitely.
    """

    raw_value = headers.get("Retry-After")
    delay: float | None = None
    parsed_retry_after = False
    if raw_value is not None:
        value = str(raw_value).strip()
        if re.fullmatch(r"[0-9]+", value):
            try:
                delay = float(int(value))
                parsed_retry_after = True
            except (ValueError, OverflowError):
                delay = None
        elif value:
            try:
                parsed = email.utils.parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                now = (
                    time.time()
                    if now_epoch_seconds is None
                    else float(now_epoch_seconds)
                )
                delay = max(0.0, parsed.timestamp() - now)
                parsed_retry_after = True
            except (TypeError, ValueError, OverflowError, OSError):
                delay = None
    if delay is None or not math.isfinite(delay) or delay < 0:
        exponent = max(0, int(retry_index) - 1)
        delay = _REMOTE_RETRY_BASE_SECONDS * (2**exponent)
        return min(float(delay), _REMOTE_RETRY_MAX_BACKOFF_SECONDS)
    if not parsed_retry_after:
        return min(float(delay), _REMOTE_RETRY_MAX_BACKOFF_SECONDS)
    return min(float(delay), _REMOTE_RETRY_AFTER_MAX_SECONDS)


def _remote_json(
    service: ServiceConfig, payload: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    readiness = _service_readiness(service)
    if readiness["status"] != "not_called":
        raise StateRagError(str(readiness.get("reason") or f"{service.name} is not configured"))
    key = str(os.environ.get(service.api_key_env, ""))
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    headers["Accept-Encoding"] = "identity"
    headers["User-Agent"] = _REMOTE_USER_AGENT
    body = _json_dumps(payload).encode("utf-8")
    pool_key, target = _remote_connection_target(service)
    started = time.monotonic()
    deadline = started + max(0.0, float(service.timeout_seconds))
    for attempt in range(1, _REMOTE_MAX_ATTEMPTS + 1):
        lease: _RemoteRetryLease | None = None
        connection: _RemoteConnection | None = None
        reusable = False
        raw = b""
        status_code = 0
        response_headers: Mapping[str, Any] = {}
        try:
            try:
                # A first request joins an existing shared throttle window, but
                # does not create one.  Retries always create/own the single
                # probe for this origin when no state remains.
                lease = _REMOTE_RETRY_COORDINATOR.acquire(
                    pool_key,
                    deadline=deadline,
                    required=attempt > 1,
                )
            except TimeoutError as exc:
                raise StateRagError(
                    f"remote {service.name} unavailable: {exc}"
                ) from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StateRagError(
                    f"remote {service.name} unavailable: request deadline exceeded"
                )
            connection = _REMOTE_CONNECTION_POOL.acquire(pool_key, remaining)
            connection.request("POST", target, body=body, headers=headers)
            response = connection.getresponse()
            status_code = int(response.status)
            response_headers = response.headers
            # Consume bounded response bytes before returning a keep-alive
            # connection.  Oversized bodies are deliberately closed, while a
            # retryable status can still be retried without trusting its body.
            raw = response.read(_REMOTE_MAX_RESPONSE_BYTES + 1)
            reusable = (
                len(raw) <= _REMOTE_MAX_RESPONSE_BYTES
                and not response.will_close
            )
        except StateRagError:
            if lease is not None:
                _REMOTE_RETRY_COORDINATOR.complete(pool_key, lease)
                lease = None
            raise
        except (http.client.HTTPException, TimeoutError, OSError) as exc:
            if lease is not None:
                _REMOTE_RETRY_COORDINATOR.complete(pool_key, lease)
                lease = None
            raise StateRagError(
                f"remote {service.name} unavailable: {exc}"
            ) from exc
        finally:
            if connection is not None:
                _REMOTE_CONNECTION_POOL.release(
                    pool_key,
                    connection,
                    reusable=reusable,
                )
                connection = None

        if status_code in _REMOTE_RETRYABLE_HTTP_STATUSES:
            delay_seconds = _remote_retry_delay_seconds(
                response_headers,
                retry_index=attempt,
            )
            # ``record_retryable`` releases the probe lease and publishes the
            # cooldown to all waiters.  Keep the lease out of the finally path
            # after this hand-off.
            _REMOTE_RETRY_COORDINATOR.record_retryable(
                pool_key,
                lease=lease,
                delay_seconds=delay_seconds,
            )
            lease = None
            if attempt >= _REMOTE_MAX_ATTEMPTS:
                raise StateRagError(
                    f"remote {service.name} HTTP {status_code} "
                    f"after {attempt} attempts"
                )
            continue

        if status_code in {301, 302, 303, 307, 308}:
            _REMOTE_RETRY_COORDINATOR.complete(pool_key, lease)
            lease = None
            raise StateRagError("remote redirects are blocked")
        if not 200 <= status_code < 300:
            _REMOTE_RETRY_COORDINATOR.complete(pool_key, lease)
            lease = None
            raise StateRagError(f"remote {service.name} HTTP {status_code}")
        if len(raw) > _REMOTE_MAX_RESPONSE_BYTES:
            _REMOTE_RETRY_COORDINATOR.complete(pool_key, lease)
            lease = None
            raise StateRagError(f"remote {service.name} response is too large")
        _REMOTE_RETRY_COORDINATOR.complete(pool_key, lease)
        lease = None
        try:
            value = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateRagError(
                f"remote {service.name} returned invalid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise StateRagError(
                f"remote {service.name} response must be a JSON object"
            )
        elapsed = int((time.monotonic() - started) * 1_000)
        status = dict(readiness)
        status.update(
            {
                "status": "ok",
                "http_status": status_code,
                "latency_ms": elapsed,
                "attempts": attempt,
                "retry_count": attempt - 1,
            }
        )
        return value, status
    raise StateRagError(
        f"remote {service.name} unavailable: request deadline exceeded"
    )


def _embedding_call(
    service: ServiceConfig, inputs: Sequence[str]
) -> tuple[list[list[float]], dict[str, Any]]:
    if not inputs:
        status = _service_readiness(service)
        status.update({"status": "not_called", "reason": "empty_input"})
        return [], status
    response, status = _remote_json(service, {"model": service.model, "input": list(inputs)})
    data = response.get("data")
    if not isinstance(data, list) or len(data) != len(inputs):
        raise StateRagError("embedding response data length does not match input length")
    ordered = sorted(
        enumerate(data),
        key=lambda pair: int(pair[1].get("index", pair[0])) if isinstance(pair[1], dict) else pair[0],
    )
    vectors: list[list[float]] = []
    for expected, (_, item) in enumerate(ordered):
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
            raise StateRagError(f"embedding response item {expected} is invalid")
        vector: list[float] = []
        for component in item["embedding"]:
            if isinstance(component, bool) or not isinstance(component, (int, float)):
                raise StateRagError("embedding contains a non-numeric component")
            number = float(component)
            if not math.isfinite(number):
                raise StateRagError("embedding contains a non-finite component")
            vector.append(number)
        if not vector or len(vector) > 65_536:
            raise StateRagError("embedding has invalid dimensions")
        vectors.append(vector)
    return vectors, status


def _rerank_call(
    service: ServiceConfig, query: str, documents: Sequence[str], top_n: int
) -> tuple[list[tuple[int, float]], dict[str, Any]]:
    response, status = _remote_json(
        service,
        {"model": service.model, "query": query, "documents": list(documents), "top_n": top_n},
    )
    results = response.get("results", response.get("data"))
    if not isinstance(results, list):
        raise StateRagError("rerank response must contain a results array")
    ranked: list[tuple[int, float]] = []
    seen: set[int] = set()
    for item in results:
        if not isinstance(item, dict):
            raise StateRagError("rerank result item must be an object")
        index = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(documents):
            raise StateRagError("rerank result index is invalid")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise StateRagError("rerank result score is invalid")
        number = float(score)
        if not math.isfinite(number) or index in seen:
            raise StateRagError("rerank result score is non-finite or duplicated")
        seen.add(index)
        ranked.append((index, number))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked[:top_n], status


def _decode_chat_completion(
    response: Mapping[str, Any],
    *,
    require_explicit_stop: bool = False,
) -> Any:
    choices = response.get("choices")
    if (
        not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], dict)
    ):
        raise StateRagError("chat completion response has no choices")
    choice = choices[0]
    raw_finish_reason = choice.get("finish_reason")
    if require_explicit_stop and (
        not isinstance(raw_finish_reason, str)
        or raw_finish_reason.strip() != "stop"
    ):
        rendered = (
            raw_finish_reason.strip()
            if isinstance(raw_finish_reason, str)
            else "missing"
        )
        raise StateRagError(
            f"chat completion finish_reason is not stop: {rendered}"
        )
    finish_reason = str(raw_finish_reason or "stop").strip()
    if finish_reason != "stop":
        raise StateRagError(
            f"chat completion finish_reason is not stop: {finish_reason}"
        )
    message = choice.get("message")
    if not isinstance(message, dict):
        raise StateRagError("chat completion response choice has no message")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {None, "text"}
        )
    if not isinstance(content, str) or not content.strip():
        raise StateRagError("chat completion returned empty content")
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateRagError(
            "chat completion content is not valid JSON"
        ) from exc


def _decode_chat_tool_call(
    response: Mapping[str, Any],
    *,
    expected_tool_name: str,
) -> Any:
    """Decode one required function call without trusting its arguments."""

    choices = response.get("choices")
    if (
        not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], dict)
    ):
        raise StateRagError("tool completion response has no choices")
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if (
        not isinstance(finish_reason, str)
        or finish_reason.strip() != "tool_calls"
    ):
        rendered = (
            finish_reason.strip()
            if isinstance(finish_reason, str)
            else "missing"
        )
        raise StateRagError(
            f"tool completion finish_reason is not tool_calls: {rendered}"
        )
    message = choice.get("message")
    if not isinstance(message, dict):
        raise StateRagError("tool completion choice has no message")
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or len(tool_calls) != 1:
        raise StateRagError(
            "tool completion must contain exactly one tool call"
        )
    tool_call = tool_calls[0]
    if not isinstance(tool_call, dict):
        raise StateRagError("tool completion call is not an object")
    if tool_call.get("type", "function") != "function":
        raise StateRagError("tool completion call type is not function")
    function = tool_call.get("function")
    if not isinstance(function, dict):
        raise StateRagError("tool completion has no function payload")
    name = function.get("name")
    if not isinstance(name, str) or name.strip() != expected_tool_name:
        raise StateRagError("tool completion function name does not match")
    arguments = function.get("arguments")
    if not isinstance(arguments, str) or not arguments.strip():
        raise StateRagError("tool completion arguments are empty")
    try:
        decoded = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise StateRagError(
            "tool completion arguments are not valid JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise StateRagError(
            "tool completion arguments must decode to a JSON object"
        )
    return decoded


def _coverage_units(
    assistant_text: str,
    deltas: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    actor_mentions = {
        str(delta.get("subject") or "").strip()
        for delta in deltas
        if str(delta.get("subject") or "").strip()
        not in {"", "故事", "世界", "world", "story"}
    }
    units: list[dict[str, Any]] = []
    for match in re.finditer(
        r"[^。！？!?\n；;，,]+[。！？!?\n；;，,]?",
        assistant_text,
    ):
        raw_quote = match.group(0)
        quote = raw_quote.strip()
        if not quote:
            continue
        if (
            _COVERAGE_BLOCK_RE.search(quote)
            or _UNCERTAIN_RE.search(quote)
            or _FUTURE_RE.search(quote)
            or _HISTORICAL_RE.search(quote)
        ):
            continue
        actor_present = any(actor in quote for actor in actor_mentions)
        if not actor_present:
            subject_match = _LOCATION_SUBJECT_RE.search(quote)
            if subject_match is not None:
                subject = subject_match.group("subject")
                for prefix in ("此刻", "此时", "当前", "现在", "当下", "如今"):
                    if subject.startswith(prefix):
                        subject = subject[len(prefix) :]
                        break
                actor_present = len(subject.strip()) >= 2
        kinds: list[str] = []
        if actor_present and _LOCATION_COVERAGE_RE.search(quote):
            kinds.append("movement")
        if _TIME_COVERAGE_RE.search(quote):
            kinds.append("time")
        if not kinds:
            continue
        leading = len(raw_quote) - len(raw_quote.lstrip())
        units.append(
            {
                "unit_id": f"unit-{len(units) + 1}",
                "start": match.start() + leading,
                "end": match.start() + leading + len(quote),
                "quote": quote,
                "event_types": kinds,
            }
        )
    return units


def _missing_coverage_units(
    units: Sequence[Mapping[str, Any]],
    deltas: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    for unit in units:
        quote = str(unit.get("quote") or "")
        required_types = {
            str(value) for value in unit.get("event_types") or []
        }
        covered_types = {
            str(delta.get("event_type") or "")
            for delta in deltas
            if str(delta.get("scope") or "current") == "current"
            and str(delta.get("knowledge_plane") or "objective") == "objective"
            and (
                str(delta.get("evidence") or "") in quote
                or quote in str(delta.get("evidence") or "")
            )
        }
        uncovered = sorted(required_types - covered_types)
        if uncovered:
            missing.append({**dict(unit), "event_types": uncovered})
    return missing


def _coverage_repair_messages(
    *,
    assistant_text: str,
    prompt: str,
    missing_units: Sequence[Mapping[str, Any]],
    schema: Mapping[str, Any],
) -> tuple[str, str]:
    allowed_types = sorted(
        {
            str(event_type)
            for unit in missing_units
            for event_type in unit.get("event_types") or []
        }
    )
    allowed_quotes = [
        {
            "unit_id": str(unit.get("unit_id") or ""),
            "event_types": list(unit.get("event_types") or []),
            "quote": str(unit.get("quote") or ""),
        }
        for unit in missing_units
    ]
    system = (
        "Repair missing typed story coverage. Return exactly one JSON object "
        "with schema_version=plot-rag-delta/v3 and deltas. Emit only the "
        f"missing event types {allowed_types}; do not repeat any other fact. "
        "Each evidence value must be one exact contiguous quote taken from "
        "ALLOWED_UNITS. For movement, use arrive when a unit establishes only "
        "the current destination and move only when the same unit explicitly "
        "contains origin and destination. Never use movement action=set. "
        "For time, put a human-readable label in value and effective_at, and "
        "omit story_coordinate unless the unit explicitly contains both a "
        "stable calendar_id and integer ordinal. Schema: "
        + _json_dumps(schema)
    )
    user = (
        "USER_PROMPT:\n<<<\n"
        + prompt
        + "\n>>>\nALLOWED_UNITS:\n"
        + _json_dumps(allowed_quotes)
        + "\nASSISTANT_TEXT:\n<<<\n"
        + assistant_text
        + "\n>>>"
    )
    return system, user


def _validate_targeted_extraction_repair(
    original: Any,
    repaired: Any,
) -> None:
    """Lock a validation repair to coordinate/time normalization only."""

    if not (
        isinstance(original, dict)
        and isinstance(repaired, dict)
        and original.get("schema_version")
        in {DELTA_V3_SCHEMA, DELTA_V4_SCHEMA}
        and repaired.get("schema_version")
        == original.get("schema_version")
        and isinstance(original.get("deltas"), list)
        and isinstance(repaired.get("deltas"), list)
        and len(original["deltas"]) == len(repaired["deltas"])
    ):
        raise StateRagError("EXTRACTION_REPAIR_CHANGED_ENVELOPE_SHAPE")
    for index, (before, after) in enumerate(
        zip(original["deltas"], repaired["deltas"])
    ):
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise StateRagError(
                f"EXTRACTION_REPAIR_CHANGED_DELTA_SHAPE:{index}"
            )
        event_type = str(before.get("event_type") or "").strip().casefold()
        if event_type in ITEM_DELTA_EVENT_TYPES:
            raise StateRagError(
                f"EXTRACTION_REPAIR_ITEM_DELTA_FORBIDDEN:{index}"
            )
        if event_type in ADVANTAGE_DELTA_EVENT_TYPES:
            raise StateRagError(
                f"EXTRACTION_REPAIR_ADVANTAGE_DELTA_FORBIDDEN:{index}"
            )
        allowed = {"story_coordinate", "effective_at"}
        if event_type == "time":
            allowed.add("value")
        for key in set(before) | set(after):
            if key in allowed:
                continue
            if before.get(key) != after.get(key):
                raise StateRagError(
                    f"EXTRACTION_REPAIR_CHANGED_UNRELATED_FIELD:{index}:{key}"
                )


def _has_protected_story_coordinate(extracted: Any) -> bool:
    """Return whether a typed candidate carries a protected coordinate."""

    return bool(
        isinstance(extracted, dict)
        and isinstance(extracted.get("deltas"), list)
        and any(
            isinstance(delta, dict)
            and str(delta.get("event_type") or "").strip().casefold()
            in (
                _POWER_EVENT_TYPES
                | ITEM_DELTA_EVENT_TYPES
                | ADVANTAGE_DELTA_EVENT_TYPES
            )
            and delta.get("story_coordinate") is not None
            for delta in extracted["deltas"]
        )
    )


def _has_power_or_item_delta(extracted: Any) -> bool:
    """Return whether an envelope contains a protected typed delta."""

    return bool(
        isinstance(extracted, dict)
        and isinstance(extracted.get("deltas"), list)
        and any(
            isinstance(delta, dict)
            and str(delta.get("event_type") or "").strip().casefold()
            in (
                _POWER_EVENT_TYPES
                | ITEM_DELTA_EVENT_TYPES
                | ADVANTAGE_DELTA_EVENT_TYPES
            )
            for delta in extracted["deltas"]
        )
    )


def _has_advantage_delta(extracted: Any) -> bool:
    return bool(
        isinstance(extracted, dict)
        and isinstance(extracted.get("deltas"), list)
        and any(
            isinstance(delta, dict)
            and str(delta.get("event_type") or "").strip().casefold()
            in ADVANTAGE_DELTA_EVENT_TYPES
            for delta in extracted["deltas"]
        )
    )


def _validation_repair_messages(
    *,
    system: str,
    user: str,
    invalid_envelope: Any,
    validation_error: BaseException,
) -> tuple[str, str]:
    """Build a full-envelope replacement request after local validation fails."""

    invalid_json = _json_dumps(invalid_envelope)
    repaired_system = (
        system
        + " The previous JSON envelope decoded successfully but failed local "
        "validation. Return exactly one complete legal replacement envelope "
        "now; do not return a patch, a supplement, prose, or a wrapper around "
        "the old answer. Rebuild every delta from ASSISTANT_TEXT and obey the "
        "authoritative schema example above. Preserve exact contiguous evidence "
        "and omit any fact that cannot be proven there. Validation error: "
        + str(validation_error)
    )
    repaired_user = (
        user
        + "\nPREVIOUS_INVALID_ENVELOPE:\n<<<\n"
        + invalid_json
        + "\n>>>"
    )
    return repaired_system, repaired_user


def _item_v4_remote_key_is_forbidden(key: str) -> bool:
    snake_case = re.sub(
        r"(?<=[A-Za-z0-9])(?=[A-Z])",
        "_",
        key.strip(),
    )
    normalized = re.sub(
        r"[^A-Za-z0-9]+",
        "_",
        snake_case,
    ).strip("_").casefold()
    if normalized in _ITEM_V4_FORBIDDEN_REMOTE_KEYS:
        return True
    if (
        normalized != "calendar_id"
        and (
            normalized == "id"
            or normalized.endswith(("_id", "_ids"))
        )
    ):
        return True
    if normalized.startswith(
        (
            "before_",
            "after_",
            "current_",
            "remaining_",
            "resulting_",
            "computed_",
            "derived_",
            "new_",
            "updated_",
        )
    ):
        return True
    return normalized.endswith(
        ("_before", "_after", "_remaining", "_result", "_total")
    )


def _validate_item_v4_json_tree(
    value: Any,
    *,
    path: str,
    depth: int = 0,
    allowed_keys: frozenset[str] = frozenset(),
) -> None:
    if depth > 16:
        raise StateRagError(f"{path} exceeds the maximum nesting depth")
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise StateRagError(f"{path} contains an invalid object key")
            if (
                raw_key not in allowed_keys
                and _item_v4_remote_key_is_forbidden(raw_key)
            ):
                raise StateRagError(
                    f"{path}.{raw_key} is a remote computed/stable-id field"
                )
            _validate_item_v4_json_tree(
                child,
                path=f"{path}.{raw_key}",
                depth=depth + 1,
            )
        return
    if isinstance(value, list):
        if len(value) > 500:
            raise StateRagError(f"{path} contains too many array items")
        for child_index, child in enumerate(value):
            _validate_item_v4_json_tree(
                child,
                path=f"{path}[{child_index}]",
                depth=depth + 1,
            )
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise StateRagError(f"{path} must contain only finite numbers")
    if value is not None and not isinstance(
        value, (str, int, float, bool)
    ):
        raise StateRagError(f"{path} contains a non-JSON value")


def _normalize_item_v4_reference(
    raw: Any,
    *,
    path: str,
    allowed_kinds: set[str] | None = None,
    evidence: str,
) -> dict[str, str]:
    if not isinstance(raw, Mapping) or set(raw) != {"kind", "mention"}:
        raise StateRagError(
            f"{path} must contain exactly kind and mention"
        )
    kind = raw.get("kind")
    mention = raw.get("mention")
    if not isinstance(kind, str) or not kind.strip():
        raise StateRagError(f"{path}.kind is invalid")
    kind = kind.strip().casefold()
    if allowed_kinds is not None and kind not in allowed_kinds:
        raise StateRagError(
            f"{path}.kind is unsupported: {kind}"
        )
    if (
        not isinstance(mention, str)
        or not mention.strip()
        or len(mention.strip()) > MAX_SUBJECT_CHARS
    ):
        raise StateRagError(f"{path}.mention is invalid")
    mention = mention.strip()
    if mention not in evidence:
        raise StateRagError(
            f"{path}.mention is not anchored in the contiguous evidence"
        )
    return {"kind": kind, "mention": mention}


def _normalize_item_v4_objects(
    raw: Any,
    *,
    event_type: str,
    evidence: str,
    index: int,
) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) > 32:
        raise StateRagError(
            f"deltas[{index}].objects must be an array with at most 32 items"
        )
    allowed_roles = _ITEM_V4_OBJECT_ROLES[event_type]
    result: list[dict[str, str]] = []
    seen_roles: set[str] = set()
    for object_index, value in enumerate(raw):
        path = f"deltas[{index}].objects[{object_index}]"
        if not isinstance(value, Mapping) or set(value) != {
            "role",
            "mention",
        }:
            raise StateRagError(
                f"{path} must contain exactly role and mention"
            )
        role_value = value.get("role")
        mention_value = value.get("mention")
        if not isinstance(role_value, str) or not role_value.strip():
            raise StateRagError(f"{path}.role is invalid")
        role = role_value.strip().casefold()
        if role not in allowed_roles:
            raise StateRagError(
                f"{path}.role is "
                f"unsupported for {event_type}: {role}"
            )
        if (
            not isinstance(mention_value, str)
            or not mention_value.strip()
            or len(mention_value.strip()) > MAX_SUBJECT_CHARS
        ):
            raise StateRagError(f"{path}.mention is invalid")
        mention = mention_value.strip()
        if mention not in evidence:
            raise StateRagError(
                f"{path}.mention is not anchored in the contiguous evidence"
            )
        if role in seen_roles:
            raise StateRagError(
                f"deltas[{index}].objects contains duplicate role: {role}"
            )
        seen_roles.add(role)
        result.append({"role": role, "mention": mention})
    return result


def _item_v4_roles(
    objects: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    return {
        str(value["role"]): str(value["mention"])
        for value in objects
    }


def _item_v4_require_roles(
    *,
    event_type: str,
    action: str,
    roles: Mapping[str, str],
    required: Sequence[str],
    index: int,
) -> None:
    missing = [role for role in required if role not in roles]
    if missing:
        raise StateRagError(
            f"deltas[{index}] {event_type}.{action} requires object roles: "
            + ", ".join(missing)
        )


def _normalize_item_v4_coordinate(
    raw: Any,
    *,
    index: int,
) -> dict[str, Any]:
    allowed = {"calendar_id", "ordinal", "label", "precision"}
    if not isinstance(raw, Mapping) or set(raw) - allowed:
        raise StateRagError(
            f"deltas[{index}].story_coordinate must contain only "
            "calendar_id, ordinal, label, and precision"
        )
    calendar_id = raw.get("calendar_id")
    ordinal = raw.get("ordinal")
    if (
        not isinstance(calendar_id, str)
        or not calendar_id.strip()
        or len(calendar_id.strip()) > MAX_FIELD_CHARS
    ):
        raise StateRagError(
            f"deltas[{index}].story_coordinate.calendar_id is invalid"
        )
    if type(ordinal) is not int:
        raise StateRagError(
            f"deltas[{index}].story_coordinate.ordinal must be an integer"
        )
    coordinate: dict[str, Any] = {
        "calendar_id": calendar_id.strip(),
        "ordinal": ordinal,
    }
    for key in ("label", "precision"):
        value = raw.get(key)
        if value is None:
            continue
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > MAX_FIELD_CHARS
        ):
            raise StateRagError(
                f"deltas[{index}].story_coordinate.{key} is invalid"
            )
        coordinate[key] = value.strip()
    return coordinate


def _normalize_item_v4_number(
    value: Any,
    *,
    path: str,
    positive: bool,
    integer: bool = False,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateRagError(f"{path} must be numeric")
    if isinstance(value, float) and not math.isfinite(value):
        raise StateRagError(f"{path} must be finite")
    if integer and type(value) is not int:
        raise StateRagError(f"{path} must be an integer")
    if positive and value <= 0:
        raise StateRagError(f"{path} must be greater than zero")
    if not positive and value < 0:
        raise StateRagError(f"{path} must be greater than or equal to zero")
    return value


def _normalize_item_v4_changes(
    raw: Any,
    *,
    event_type: str,
    action: str,
    subject_kind: str,
    roles: Mapping[str, str],
    assistant_text: str,
    evidence: str,
    index: int,
    min_confidence: float,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise StateRagError(f"deltas[{index}].changes must be an object")
    unsupported = set(raw) - _ITEM_V4_CHANGE_KEYS[event_type]
    if unsupported:
        raise StateRagError(
            f"deltas[{index}].changes contains unsupported fields for "
            f"{event_type}: {', '.join(sorted(unsupported))}"
        )
    action_allowed: set[str] | None = None
    if event_type == "item_runtime":
        action_allowed = _ITEM_V4_RUNTIME_CHANGE_KEYS_BY_ACTION[action]
    elif event_type == "item_function_runtime":
        action_allowed = _ITEM_V4_FUNCTION_RUNTIME_CHANGE_KEYS_BY_ACTION[
            action
        ]
    if action_allowed is not None:
        action_unsupported = set(raw) - action_allowed
        if action_unsupported:
            raise StateRagError(
                f"deltas[{index}].changes contains unsupported fields for "
                f"{event_type}.{action}: "
                + ", ".join(sorted(action_unsupported))
            )
    changes = dict(raw)
    allowed_runtime_keys = (
        frozenset({"remaining_charges", "cooldown_until"})
        if event_type == "item_function_runtime"
        else frozenset()
    )
    _validate_item_v4_json_tree(
        changes,
        path=f"deltas[{index}].changes",
        allowed_keys=allowed_runtime_keys,
    )
    if "reason" in changes:
        reason = changes["reason"]
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason.strip()) > MAX_EVIDENCE_CHARS
        ):
            raise StateRagError(
                f"deltas[{index}].changes.reason is invalid"
            )
        reason = reason.strip()
        if reason not in evidence:
            raise StateRagError(
                f"deltas[{index}].changes.reason is not explicit in evidence"
            )
        changes["reason"] = reason

    if "quantity" in changes:
        changes["quantity"] = _normalize_item_v4_number(
            changes["quantity"],
            path=f"deltas[{index}].changes.quantity",
            positive=True,
        )
        if (
            subject_kind == "item_instance"
            and changes["quantity"] not in {1, 1.0}
        ):
            raise StateRagError(
                f"deltas[{index}] an item_instance quantity must be 1"
            )

    if event_type == "item_spec":
        definition = changes.get("definition")
        if definition is None:
            if action != "deprecate":
                raise StateRagError(
                    f"deltas[{index}].changes.definition is required"
                )
            definition = {}
        if not isinstance(definition, Mapping):
            raise StateRagError(
                f"deltas[{index}].changes.definition must be an object"
            )
        unsupported_definition = (
            set(definition) - _ITEM_V4_DEFINITION_KEYS[subject_kind]
        )
        if unsupported_definition:
            raise StateRagError(
                f"deltas[{index}].changes.definition contains unsupported "
                f"fields for {subject_kind}: "
                + ", ".join(sorted(unsupported_definition))
            )
        definition = dict(definition)
        if action != "deprecate" and not definition:
            raise StateRagError(
                f"deltas[{index}].changes.definition cannot be empty"
            )
        if subject_kind == "item_definition":
            stack_policy = definition.get("stack_policy")
            if stack_policy is not None and stack_policy not in {
                "non_stackable",
                "homogeneous",
                "lot",
            }:
                raise StateRagError(
                    f"deltas[{index}].changes.definition.stack_policy "
                    "is invalid"
                )
            uniqueness_policy = definition.get("uniqueness_policy")
            if uniqueness_policy is not None and uniqueness_policy not in {
                "ordinary",
                "unique_instance",
                "unique_definition",
            }:
                raise StateRagError(
                    f"deltas[{index}].changes.definition.uniqueness_policy "
                    "is invalid"
                )
        elif subject_kind == "function_definition":
            effect_owner = definition.get("effect_owner")
            if effect_owner is not None and effect_owner not in {
                "inline",
                "ability_bridge",
            }:
                raise StateRagError(
                    f"deltas[{index}].changes.definition.effect_owner "
                    "is invalid"
                )
            granted_mentions = definition.get("granted_abilities") or []
            has_granted_ability = bool(
                granted_mentions or "ability" in roles
            )
            if effect_owner == "inline" and has_granted_ability:
                raise StateRagError(
                    f"deltas[{index}] inline item functions cannot also "
                    "grant bridged abilities"
                )
            if effect_owner == "ability_bridge":
                if not has_granted_ability:
                    raise StateRagError(
                        f"deltas[{index}] ability_bridge requires an explicit "
                        "granted ability mention"
                    )
                duplicate_fields = [
                    key
                    for key in (
                        "inline_effects",
                        "costs",
                        "cooldown",
                    )
                    if definition.get(key)
                ]
                if duplicate_fields:
                    raise StateRagError(
                        f"deltas[{index}] ability_bridge effects/costs/"
                        "cooldown belong to the ability definition: "
                        + ", ".join(duplicate_fields)
                    )
        elif (
            "enabled" in definition
            and type(definition["enabled"]) is not bool
        ):
            raise StateRagError(
                f"deltas[{index}].changes.definition.enabled must be boolean"
            )
        for key in ("default_functions", "granted_abilities", "tags"):
            if key not in definition:
                continue
            values = definition[key]
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value.strip()
                for value in values
            ):
                raise StateRagError(
                    f"deltas[{index}].changes.definition.{key} must be "
                    "an array of non-empty mentions"
                )
            definition[key] = list(
                dict.fromkeys(value.strip() for value in values)
            )
            if key in {"default_functions", "granted_abilities"}:
                unanchored = [
                    value
                    for value in definition[key]
                    if value not in evidence
                ]
                if unanchored:
                    raise StateRagError(
                        f"deltas[{index}].changes.definition.{key} contains "
                        "mentions not anchored in evidence: "
                        + ", ".join(unanchored)
                    )
        for key in (
            "capacity",
            "unit_bulk",
            "max_durability",
            "max_energy",
            "charges",
            "durability_cost",
        ):
            if key in definition:
                definition[key] = _normalize_item_v4_number(
                    definition[key],
                    path=f"deltas[{index}].changes.definition.{key}",
                    positive=False,
                )
        if "cooldown" in definition:
            definition["cooldown"] = _normalize_item_v4_number(
                definition["cooldown"],
                path=f"deltas[{index}].changes.definition.cooldown",
                positive=False,
                integer=True,
            )
        changes["definition"] = definition

    for key in (
        "batch",
        "target_batch",
        "attributes",
        "terms",
        "provenance",
        "state",
    ):
        if key in changes and not isinstance(changes[key], Mapping):
            raise StateRagError(
                f"deltas[{index}].changes.{key} must be an object"
            )
        if key in changes:
            changes[key] = dict(changes[key])

    for key in ("instance_name", "serial_or_mark"):
        if key not in changes:
            continue
        value = changes[key]
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > MAX_SUBJECT_CHARS
        ):
            raise StateRagError(
                f"deltas[{index}].changes.{key} is invalid"
            )
        value = value.strip()
        if value not in evidence:
            raise StateRagError(
                f"deltas[{index}].changes.{key} is not explicit in evidence"
            )
        changes[key] = value

    for key in ("custody_status", "unlock_state"):
        if key not in changes:
            continue
        value = changes[key]
        allowed_values = (
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
            if key == "custody_status"
            else {"locked", "unlocked", "suppressed"}
        )
        if not isinstance(value, str) or value not in allowed_values:
            raise StateRagError(
                f"deltas[{index}].changes.{key} is invalid"
            )

    if (
        "unique" in changes
        and type(changes["unique"]) is not bool
        and changes["unique"] != "unknown"
    ):
        raise StateRagError(
            f"deltas[{index}].changes.unique must be boolean or 'unknown'"
        )

    for key in ("sealed", "damaged", "destroyed", "active", "enabled"):
        if key in changes and type(changes[key]) is not bool:
            raise StateRagError(
                f"deltas[{index}].changes.{key} must be boolean"
            )

    for key in (
        "durability",
        "max_durability",
        "energy",
        "max_energy",
        "remaining_charges",
    ):
        if key in changes:
            changes[key] = _normalize_item_v4_number(
                changes[key],
                path=f"deltas[{index}].changes.{key}",
                positive=False,
            )

    if "cooldown_until" in changes:
        changes["cooldown_until"] = _normalize_item_v4_coordinate(
            changes["cooldown_until"],
            index=index,
        )

    if "slot_key" in changes:
        slot_key = changes["slot_key"]
        if (
            not isinstance(slot_key, str)
            or not slot_key.strip()
            or len(slot_key.strip()) > MAX_FIELD_CHARS
        ):
            raise StateRagError(
                f"deltas[{index}].changes.slot_key is invalid"
            )
        changes["slot_key"] = slot_key.strip()

    if event_type in {
        "item_runtime",
        "item_function_runtime",
        "item_use",
    }:
        delta = changes.get("delta", {})
        if not isinstance(delta, Mapping):
            raise StateRagError(
                f"deltas[{index}].changes.delta must be an object"
            )
        unsupported_delta = set(delta) - _ITEM_V4_DELTA_KEYS
        if unsupported_delta:
            raise StateRagError(
                f"deltas[{index}].changes.delta contains unsupported fields: "
                + ", ".join(sorted(unsupported_delta))
            )
        normalized_delta: dict[str, int | float] = {}
        for key, value in delta.items():
            normalized_delta[key] = _normalize_item_v4_number(
                value,
                path=f"deltas[{index}].changes.delta.{key}",
                positive=not (
                    event_type == "item_function_runtime"
                    and action == "set_charges"
                    and key == "charges"
                ),
                integer=key == "cooldown",
            )
        if "delta" in changes:
            changes["delta"] = normalized_delta
        allowed_delta_keys = (
            {"energy"}
            if event_type == "item_runtime"
            and action in {"charge", "discharge"}
            else {"durability"}
            if event_type == "item_runtime"
            and action in {"repair", "damage"}
            else {"charges"}
            if event_type == "item_function_runtime"
            and action == "set_charges"
            else _ITEM_V4_DELTA_KEYS
        )
        action_delta_unsupported = (
            set(normalized_delta) - allowed_delta_keys
        )
        if action_delta_unsupported:
            raise StateRagError(
                f"deltas[{index}].changes.delta contains unsupported fields "
                f"for {event_type}.{action}: "
                + ", ".join(sorted(action_delta_unsupported))
            )
        required_delta = {
            "charge": "energy",
            "discharge": "energy",
            "repair": "durability",
            "damage": "durability",
        }.get(action)
        if required_delta and required_delta not in normalized_delta:
            raise StateRagError(
                f"deltas[{index}] {action} requires explicit "
                f"changes.delta.{required_delta}"
            )
        if (
            event_type == "item_function_runtime"
            and action == "set_charges"
            and (
                ("remaining_charges" in changes)
                == ("charges" in normalized_delta)
            )
        ):
            raise StateRagError(
                f"deltas[{index}] set_charges requires exactly one of "
                "changes.remaining_charges or changes.delta.charges"
            )
        if (
            event_type == "item_function_runtime"
            and action == "set_cooldown"
            and "cooldown_until" not in changes
        ):
            raise StateRagError(
                f"deltas[{index}] set_cooldown requires explicit "
                "changes.cooldown_until"
            )

    if event_type == "item_observation":
        observation = changes.get("observation")
        if not isinstance(observation, Mapping) or not observation:
            raise StateRagError(
                f"deltas[{index}].changes.observation must be a non-empty object"
            )
        changes["observation"] = dict(observation)

    if event_type == "item_use" and "observed_effects" in changes:
        observed = changes["observed_effects"]
        if not isinstance(observed, (Mapping, list)) or not observed:
            raise StateRagError(
                f"deltas[{index}].changes.observed_effects must be a "
                "non-empty object or array"
            )
        changes["observed_effects"] = (
            dict(observed) if isinstance(observed, Mapping) else list(observed)
        )

    if event_type == "item_correction":
        replacement = changes.get("replacement")
        if action == "retract":
            if replacement is not None:
                raise StateRagError(
                    f"deltas[{index}] item_correction.retract cannot "
                    "contain changes.replacement"
                )
        else:
            if not isinstance(replacement, Mapping):
                raise StateRagError(
                    f"deltas[{index}] item_correction.{action} requires "
                    "changes.replacement"
                )
            normalized_replacement = normalize_item_extraction_candidate(
                replacement,
                assistant_text,
                min_confidence=min_confidence,
                index=index,
            )
            if normalized_replacement["event_type"] == "item_correction":
                raise StateRagError(
                    f"deltas[{index}] item_correction replacement cannot "
                    "be another correction"
                )
            changes["replacement"] = normalized_replacement

    return changes


def normalize_item_extraction_candidate(
    raw: Mapping[str, Any],
    assistant_text: str,
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    index: int = 0,
) -> dict[str, Any]:
    """Validate and normalize one neutral ``plot-rag-delta/v4`` item candidate.

    The remote model supplies mentions, an action, explicit change magnitudes,
    a comparable story coordinate, and one contiguous quote.  Stable IDs and
    before/after state are deliberately absent so the continuity adapter and
    reducer remain the sole authority for identity and computed state.
    """

    if isinstance(raw, Mapping) and "schema_version" in raw:
        if raw.get("schema_version") != DELTA_V4_SCHEMA:
            raise StateRagError(
                f"deltas[{index}].schema_version must be {DELTA_V4_SCHEMA}"
            )
        raw = _raw_item_candidate_view(raw)
    required = {
        "event_type",
        "action",
        "subject",
        "objects",
        "changes",
        "scope",
        "story_coordinate",
        "knowledge_plane",
        "confidence",
        "evidence",
    }
    optional = {"effective_at", "ambiguity"}
    keys = set(raw) if isinstance(raw, Mapping) else set()
    if (
        not isinstance(raw, Mapping)
        or not required.issubset(keys)
        or not keys.issubset(required | optional)
    ):
        raise StateRagError(
            f"deltas[{index}] must contain the closed v4 item candidate fields"
        )
    event_type_value = raw.get("event_type")
    if not isinstance(event_type_value, str):
        raise StateRagError(f"deltas[{index}].event_type is invalid")
    event_type = event_type_value.strip().casefold()
    if event_type not in ITEM_DELTA_EVENT_TYPES:
        raise StateRagError(
            f"deltas[{index}].event_type is not a v4 item event: {event_type}"
        )
    action_value = raw.get("action")
    if not isinstance(action_value, str):
        raise StateRagError(f"deltas[{index}].action is invalid")
    action = action_value.strip().casefold()
    allowed_actions = _ITEM_V4_ACTIONS[event_type]
    if action == event_type and len(allowed_actions) == 1:
        action = next(iter(allowed_actions))
    if action not in allowed_actions:
        raise StateRagError(
            f"deltas[{index}].action is unsupported for {event_type}: {action}"
        )

    evidence = raw.get("evidence")
    if (
        not isinstance(evidence, str)
        or not evidence
        or evidence != evidence.strip()
        or len(evidence) > MAX_EVIDENCE_CHARS
    ):
        raise StateRagError(f"deltas[{index}].evidence is invalid")
    if evidence not in assistant_text:
        raise StateRagError(
            f"deltas[{index}].evidence is not one exact contiguous quote "
            "from assistant_text"
        )

    subject = _normalize_item_v4_reference(
        raw.get("subject"),
        path=f"deltas[{index}].subject",
        allowed_kinds=_ITEM_V4_SUBJECT_KINDS[event_type],
        evidence=evidence,
    )
    objects = _normalize_item_v4_objects(
        raw.get("objects"),
        event_type=event_type,
        evidence=evidence,
        index=index,
    )
    roles = _item_v4_roles(objects)

    if event_type == "item_spec":
        if action == "supersede":
            _item_v4_require_roles(
                event_type=event_type,
                action=action,
                roles=roles,
                required=("supersedes_spec",),
                index=index,
            )
        if subject["kind"] == "function_definition":
            _item_v4_require_roles(
                event_type=event_type,
                action=action,
                roles=roles,
                required=("item_definition",),
                index=index,
            )
        elif subject["kind"] == "function_binding":
            _item_v4_require_roles(
                event_type=event_type,
                action=action,
                roles=roles,
                required=("function",),
                index=index,
            )
            targets = {
                "item_definition",
                "item_instance",
                "item_stack",
            }.intersection(roles)
            if len(targets) != 1:
                raise StateRagError(
                    f"deltas[{index}] function_binding requires exactly one "
                    "item_definition, item_instance, or item_stack object"
                )
    elif event_type == "item_instance":
        if action == "instantiate":
            _item_v4_require_roles(
                event_type=event_type,
                action=action,
                roles=roles,
                required=("item_definition",),
                index=index,
            )
            if (
                subject["kind"] == "item_stack"
                and "quantity" not in (raw.get("changes") or {})
            ):
                raise StateRagError(
                    f"deltas[{index}] item_stack instantiate requires "
                    "explicit changes.quantity"
                )
        elif action in {"split", "merge"}:
            if subject["kind"] != "item_stack":
                raise StateRagError(
                    f"deltas[{index}] {action} requires an item_stack subject"
                )
            _item_v4_require_roles(
                event_type=event_type,
                action=action,
                roles=roles,
                required=("source_stack", "target_stack"),
                index=index,
            )
            if "quantity" not in (raw.get("changes") or {}):
                raise StateRagError(
                    f"deltas[{index}] {action} requires explicit "
                    "changes.quantity"
                )
    elif event_type == "item_custody":
        if action == "transfer_title":
            _item_v4_require_roles(
                event_type=event_type,
                action=action,
                roles=roles,
                required=("from_legal_owner", "to_legal_owner"),
                index=index,
            )
        elif action in {
            "acquire",
            "handover",
            "loan",
            "return",
            "seize",
            "store",
            "retrieve",
            "recover",
        } and not {
            "to_custodian",
            "to_carrier",
            "to_container",
            "to_location",
        }.intersection(roles) and not (
            action == "acquire"
            and (raw.get("changes") or {}).get("custody_status")
            == "unknown"
        ):
            raise StateRagError(
                f"deltas[{index}] item_custody.{action} requires one "
                "explicit destination custody anchor"
            )
    elif event_type == "item_runtime":
        if action in {"equip", "bind"}:
            _item_v4_require_roles(
                event_type=event_type,
                action=action,
                roles=roles,
                required=("actor",),
                index=index,
            )
        if action == "equip" and not (
            "slot" in roles
            or str(
                (raw.get("changes") or {}).get("slot_key") or ""
            ).strip()
        ):
            raise StateRagError(
                f"deltas[{index}] item_runtime.equip requires an explicit slot"
            )
        if action in {"unlock_function", "suppress_function"}:
            _item_v4_require_roles(
                event_type=event_type,
                action=action,
                roles=roles,
                required=("function",),
                index=index,
            )
    elif event_type == "item_function_runtime":
        _item_v4_require_roles(
            event_type=event_type,
            action=action,
            roles=roles,
            required=("function",),
            index=index,
        )
    elif event_type == "item_use":
        _item_v4_require_roles(
            event_type=event_type,
            action=action,
            roles=roles,
            required=("actor", "function"),
            index=index,
        )
    elif event_type == "item_observation":
        if raw.get("knowledge_plane") == "actor_belief":
            _item_v4_require_roles(
                event_type=event_type,
                action=action,
                roles=roles,
                required=("observer",),
                index=index,
            )
    elif event_type == "item_correction":
        _item_v4_require_roles(
            event_type=event_type,
            action=action,
            roles=roles,
            required=("target_event",),
            index=index,
        )

    scope = raw.get("scope")
    if not isinstance(scope, str) or scope not in _ITEM_V4_SCOPES:
        raise StateRagError(f"deltas[{index}].scope is invalid")
    if scope == "current" and _FUTURE_RE.search(evidence):
        scope = "planned"
    elif scope == "current" and _HISTORICAL_RE.search(evidence):
        scope = "historical"
    if event_type != "item_spec" and scope == "timeless":
        raise StateRagError(
            f"deltas[{index}].scope=timeless is reserved for item_spec"
        )

    knowledge_plane = raw.get("knowledge_plane")
    if (
        not isinstance(knowledge_plane, str)
        or knowledge_plane not in POWER_KNOWLEDGE_PLANES
    ):
        raise StateRagError(
            f"deltas[{index}].knowledge_plane is invalid"
        )
    if knowledge_plane == "author_plan" and scope == "current":
        scope = "planned"
    if (
        event_type not in {"item_observation", "item_correction"}
        and knowledge_plane
        in {"actor_belief", "public_narrative", "reader_disclosed"}
    ):
        raise StateRagError(
            f"deltas[{index}] {knowledge_plane} item claims must use "
            "item_observation instead of a state-changing item event"
        )

    confidence_value = raw.get("confidence")
    if (
        isinstance(confidence_value, bool)
        or not isinstance(confidence_value, (int, float))
    ):
        raise StateRagError(
            f"deltas[{index}].confidence must be numeric"
        )
    confidence = float(confidence_value)
    if (
        not math.isfinite(confidence)
        or not float(min_confidence) <= confidence <= 1.0
    ):
        raise StateRagError(
            f"deltas[{index}].confidence is below {min_confidence} or above 1"
        )

    coordinate = _normalize_item_v4_coordinate(
        raw.get("story_coordinate"),
        index=index,
    )
    effective_at = raw.get("effective_at")
    if effective_at is not None:
        if isinstance(effective_at, bool) or not isinstance(
            effective_at, (str, int, float)
        ):
            raise StateRagError(
                f"deltas[{index}].effective_at must be a string or number"
            )
        if isinstance(effective_at, float) and not math.isfinite(effective_at):
            raise StateRagError(
                f"deltas[{index}].effective_at must be finite"
            )
        if isinstance(effective_at, str) and not effective_at.strip():
            raise StateRagError(
                f"deltas[{index}].effective_at must not be empty"
            )

    changes = _normalize_item_v4_changes(
        raw.get("changes"),
        event_type=event_type,
        action=action,
        subject_kind=subject["kind"],
        roles=roles,
        assistant_text=assistant_text,
        evidence=evidence,
        index=index,
        min_confidence=float(min_confidence),
    )
    ambiguity = raw.get("ambiguity")
    _validate_item_v4_json_tree(
        ambiguity,
        path=f"deltas[{index}].ambiguity",
    )
    normalized = {
        "schema_version": DELTA_V4_SCHEMA,
        "event_type": event_type,
        "action": action,
        "subject": subject,
        "objects": objects,
        "changes": changes,
        "scope": scope,
        "effective_at": effective_at,
        "story_coordinate": coordinate,
        "knowledge_plane": knowledge_plane,
        "ambiguity": ambiguity,
        "confidence": confidence,
        "evidence": evidence,
    }
    try:
        rendered = _json_dumps(normalized)
    except (TypeError, ValueError) as exc:
        raise StateRagError(
            f"deltas[{index}] contains non-strict JSON"
        ) from exc
    if len(rendered) > MAX_VALUE_JSON_CHARS:
        raise StateRagError(f"deltas[{index}] payload is too large")
    return normalized


def _normalize_advantage_v4_objects(
    raw: Any,
    *,
    event_type: str,
    evidence: str,
    index: int,
) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) > 32:
        raise StateRagError(
            f"deltas[{index}].objects must be an array with at most 32 items"
        )
    allowed_roles = _ADVANTAGE_V4_OBJECT_ROLES[event_type]
    repeatable = _ADVANTAGE_V4_REPEATABLE_ROLES.get(event_type, set())
    result: list[dict[str, str]] = []
    seen_roles: set[str] = set()
    for object_index, value in enumerate(raw):
        path = f"deltas[{index}].objects[{object_index}]"
        if not isinstance(value, Mapping) or set(value) != {
            "role",
            "mention",
        }:
            raise StateRagError(
                f"{path} must contain exactly role and mention"
            )
        role_value = value.get("role")
        mention_value = value.get("mention")
        if not isinstance(role_value, str) or not role_value.strip():
            raise StateRagError(f"{path}.role is invalid")
        role = role_value.strip().casefold()
        if role not in allowed_roles:
            raise StateRagError(
                f"{path}.role is unsupported for {event_type}: {role}"
            )
        if (
            not isinstance(mention_value, str)
            or not mention_value.strip()
            or len(mention_value.strip()) > MAX_SUBJECT_CHARS
        ):
            raise StateRagError(f"{path}.mention is invalid")
        mention = mention_value.strip()
        if mention not in evidence:
            raise StateRagError(
                f"{path}.mention is not anchored in the contiguous evidence"
            )
        if role in seen_roles and role not in repeatable:
            raise StateRagError(
                f"deltas[{index}].objects contains duplicate role: {role}"
            )
        seen_roles.add(role)
        result.append({"role": role, "mention": mention})
    return result


def _advantage_v4_roles(
    objects: Sequence[Mapping[str, Any]],
) -> dict[str, list[str]]:
    roles: dict[str, list[str]] = {}
    for value in objects:
        roles.setdefault(str(value["role"]), []).append(str(value["mention"]))
    return roles


def _advantage_v4_require_roles(
    *,
    event_type: str,
    action: str,
    roles: Mapping[str, Sequence[str]],
    required: Sequence[str],
    index: int,
) -> None:
    missing = [role for role in required if not roles.get(role)]
    if missing:
        raise StateRagError(
            f"deltas[{index}] {event_type}.{action} requires object roles: "
            + ", ".join(missing)
        )


def _normalize_advantage_v4_text(
    value: Any,
    *,
    path: str,
    required: bool = False,
) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > MAX_SUBJECT_CHARS
    ):
        raise StateRagError(f"{path} is invalid")
    return value.strip()


def _normalize_advantage_v4_string_list(
    value: Any,
    *,
    path: str,
) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 500
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item.strip()) > MAX_SUBJECT_CHARS
            for item in value
        )
    ):
        raise StateRagError(f"{path} must be an array of non-empty strings")
    return [item.strip() for item in value]


def _normalize_advantage_v4_changes(
    raw: Any,
    *,
    event_type: str,
    action: str,
    subject_kind: str,
    roles: Mapping[str, Sequence[str]],
    assistant_text: str,
    index: int,
    min_confidence: float,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise StateRagError(f"deltas[{index}].changes must be an object")
    unsupported = set(raw) - _ADVANTAGE_V4_CHANGE_KEYS[event_type]
    if unsupported:
        raise StateRagError(
            f"deltas[{index}].changes contains unsupported fields for "
            f"{event_type}: {', '.join(sorted(unsupported))}"
        )
    changes = dict(raw)
    _validate_item_v4_json_tree(
        changes,
        path=f"deltas[{index}].changes",
    )

    if event_type == "advantage_spec":
        title = _normalize_advantage_v4_text(
            changes.get("title"),
            path=f"deltas[{index}].changes.title",
            required=action == "define",
        )
        if title is not None:
            changes["title"] = title
        anchor_type = changes.get("anchor_type")
        if anchor_type is None and action == "define":
            raise StateRagError(
                f"deltas[{index}].changes.anchor_type is required"
            )
        if anchor_type is not None:
            anchor_type = _normalize_advantage_v4_text(
                anchor_type,
                path=f"deltas[{index}].changes.anchor_type",
                required=True,
            )
            if anchor_type not in ADVANTAGE_ANCHOR_TYPES:
                raise StateRagError(
                    f"deltas[{index}].changes.anchor_type is invalid"
                )
            changes["anchor_type"] = anchor_type
        if "profiles" in changes:
            changes["profiles"] = _normalize_advantage_v4_string_list(
                changes["profiles"],
                path=f"deltas[{index}].changes.profiles",
            )
        for key in ("acquisition_mode", "uniqueness"):
            if key in changes:
                changes[key] = _normalize_advantage_v4_text(
                    changes[key],
                    path=f"deltas[{index}].changes.{key}",
                    required=True,
                )
        if "definition" in changes and not isinstance(
            changes["definition"], Mapping
        ):
            raise StateRagError(
                f"deltas[{index}].changes.definition must be an object"
            )

    elif event_type == "advantage_anchor":
        anchor_type = changes.get("anchor_type")
        if anchor_type is None and action == "define":
            raise StateRagError(
                f"deltas[{index}].changes.anchor_type is required"
            )
        if anchor_type is not None:
            anchor_type = _normalize_advantage_v4_text(
                anchor_type,
                path=f"deltas[{index}].changes.anchor_type",
                required=True,
            )
            if anchor_type not in ADVANTAGE_ANCHOR_TYPES:
                raise StateRagError(
                    f"deltas[{index}].changes.anchor_type is invalid"
                )
            changes["anchor_type"] = anchor_type
        if "binding_state" in changes:
            binding_state = _normalize_advantage_v4_text(
                changes["binding_state"],
                path=f"deltas[{index}].changes.binding_state",
                required=True,
            )
            if binding_state not in {
                "unbound",
                "bound",
                "dormant",
                "sealed",
                "contested",
                "released",
            }:
                raise StateRagError(
                    f"deltas[{index}].changes.binding_state is invalid"
                )
            changes["binding_state"] = binding_state
        if "transfer_rule" in changes and not isinstance(
            changes["transfer_rule"], (str, Mapping)
        ):
            raise StateRagError(
                f"deltas[{index}].changes.transfer_rule must be text or an object"
            )
        if isinstance(changes.get("transfer_rule"), str):
            changes["transfer_rule"] = _normalize_advantage_v4_text(
                changes["transfer_rule"],
                path=f"deltas[{index}].changes.transfer_rule",
                required=True,
            )
        if "attributes" in changes and not isinstance(
            changes["attributes"], Mapping
        ):
            raise StateRagError(
                f"deltas[{index}].changes.attributes must be an object"
            )

    elif event_type == "advantage_module":
        for key in ("title", "kind"):
            normalized_text = _normalize_advantage_v4_text(
                changes.get(key),
                path=f"deltas[{index}].changes.{key}",
                required=action == "define",
            )
            if normalized_text is not None:
                changes[key] = normalized_text
        for key in ("stage",):
            if key in changes:
                changes[key] = _normalize_advantage_v4_text(
                    changes[key],
                    path=f"deltas[{index}].changes.{key}",
                    required=True,
                )
        if "module_status" in changes:
            module_status = _normalize_advantage_v4_text(
                changes["module_status"],
                path=f"deltas[{index}].changes.module_status",
                required=True,
            )
            if module_status not in {
                "locked",
                "available",
                "enabled",
                "suppressed",
                "deprecated",
                "superseded",
            }:
                raise StateRagError(
                    f"deltas[{index}].changes.module_status is invalid"
                )
            changes["module_status"] = module_status

    elif event_type == "advantage_activate":
        if "stage" in changes:
            changes["stage"] = _normalize_advantage_v4_text(
                changes["stage"],
                path=f"deltas[{index}].changes.stage",
                required=True,
            )

    elif event_type in {
        "advantage_trigger",
        "advantage_use",
        "advantage_reward",
        "advantage_cost",
    }:
        if "cooldown" in changes:
            cooldown = changes["cooldown"]
            if isinstance(cooldown, Mapping):
                changes["cooldown"] = _normalize_item_v4_coordinate(
                    cooldown,
                    index=index,
                )
            else:
                changes["cooldown"] = _normalize_item_v4_number(
                    cooldown,
                    path=f"deltas[{index}].changes.cooldown",
                    positive=False,
                    integer=True,
                )
        for key in (
            "pollution_delta",
            "exposure_delta",
            "debt_delta",
        ):
            if key in changes:
                changes[key] = _normalize_item_v4_number(
                    changes[key],
                    path=f"deltas[{index}].changes.{key}",
                    positive=False,
                )

    elif event_type == "advantage_upgrade":
        changes["to_stage"] = _normalize_advantage_v4_text(
            changes.get("to_stage"),
            path=f"deltas[{index}].changes.to_stage",
            required=True,
        )
        if "max_charges" in changes:
            changes["max_charges"] = _normalize_item_v4_number(
                changes["max_charges"],
                path=f"deltas[{index}].changes.max_charges",
                positive=False,
            )

    elif event_type == "advantage_reveal":
        claim = changes.get("claim")
        if claim is None or claim == "" or claim == {} or claim == []:
            raise StateRagError(
                f"deltas[{index}].changes.claim is required"
            )
        changes["reveal_stage"] = _normalize_advantage_v4_text(
            changes.get("reveal_stage"),
            path=f"deltas[{index}].changes.reveal_stage",
            required=True,
        )
        if "status" in changes:
            status = _normalize_advantage_v4_text(
                changes["status"],
                path=f"deltas[{index}].changes.status",
                required=True,
            )
            if status not in {"canon", "planned", "rumor", "misread"}:
                raise StateRagError(
                    f"deltas[{index}].changes.status is invalid"
                )
            changes["status"] = status
            if status == "misread" and not roles.get("misread_of"):
                raise StateRagError(
                    f"deltas[{index}] advantage_reveal.reveal with "
                    "status=misread requires object role: misread_of"
                )
        if "record_ledger" in changes and type(
            changes["record_ledger"]
        ) is not bool:
            raise StateRagError(
                f"deltas[{index}].changes.record_ledger must be boolean"
            )

    elif event_type == "advantage_contract":
        if "contract_status" in changes:
            changes["contract_status"] = _normalize_advantage_v4_text(
                changes["contract_status"],
                path=f"deltas[{index}].changes.contract_status",
                required=True,
            )
        for key in ("trust_delta", "debt_delta"):
            if key in changes:
                value = changes[key]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or (isinstance(value, float) and not math.isfinite(value))
                ):
                    raise StateRagError(
                        f"deltas[{index}].changes.{key} must be finite numeric"
                    )

    elif event_type == "advantage_correction":
        replacement = changes.get("replacement")
        if action == "retract":
            if replacement is not None:
                raise StateRagError(
                    f"deltas[{index}] retract advantage_correction "
                    "cannot contain changes.replacement"
                )
        else:
            if not isinstance(replacement, Mapping):
                raise StateRagError(
                    f"deltas[{index}].changes.replacement is required"
                )
            replacement_event_type = str(
                replacement.get("event_type") or ""
            ).strip().casefold()
            if (
                replacement_event_type not in ADVANTAGE_DELTA_EVENT_TYPES
                or replacement_event_type == "advantage_correction"
            ):
                raise StateRagError(
                    f"deltas[{index}].changes.replacement must be a "
                    "non-correction Advantage candidate"
                )
            changes["replacement"] = (
                normalize_advantage_extraction_candidate(
                    replacement,
                    assistant_text,
                    min_confidence=min_confidence,
                    index=index,
                )
            )

    return changes


def normalize_advantage_extraction_candidate(
    raw: Mapping[str, Any],
    assistant_text: str,
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    index: int = 0,
) -> dict[str, Any]:
    """Validate one neutral ``plot-rag-delta/v4`` Advantage candidate."""

    if isinstance(raw, Mapping) and "schema_version" in raw:
        if raw.get("schema_version") != DELTA_V4_SCHEMA:
            raise StateRagError(
                f"deltas[{index}].schema_version must be {DELTA_V4_SCHEMA}"
            )
        raw = _raw_advantage_candidate_view(raw)
    required = {
        "event_type",
        "action",
        "subject",
        "objects",
        "changes",
        "scope",
        "story_coordinate",
        "knowledge_plane",
        "confidence",
        "evidence",
    }
    optional = {"effective_at", "ambiguity"}
    keys = set(raw) if isinstance(raw, Mapping) else set()
    if (
        not isinstance(raw, Mapping)
        or not required.issubset(keys)
        or not keys.issubset(required | optional)
    ):
        raise StateRagError(
            f"deltas[{index}] must contain the closed v4 Advantage "
            "candidate fields"
        )
    event_type_value = raw.get("event_type")
    if not isinstance(event_type_value, str):
        raise StateRagError(f"deltas[{index}].event_type is invalid")
    event_type = event_type_value.strip().casefold()
    if event_type not in ADVANTAGE_DELTA_EVENT_TYPES:
        raise StateRagError(
            f"deltas[{index}].event_type is not a v4 Advantage event: "
            f"{event_type}"
        )
    action_value = raw.get("action")
    if not isinstance(action_value, str):
        raise StateRagError(f"deltas[{index}].action is invalid")
    action = action_value.strip().casefold()
    if action not in _ADVANTAGE_V4_ACTIONS[event_type]:
        raise StateRagError(
            f"deltas[{index}].action is unsupported for "
            f"{event_type}: {action}"
        )

    evidence = raw.get("evidence")
    if (
        not isinstance(evidence, str)
        or not evidence
        or evidence != evidence.strip()
        or len(evidence) > MAX_EVIDENCE_CHARS
    ):
        raise StateRagError(f"deltas[{index}].evidence is invalid")
    if evidence not in assistant_text:
        raise StateRagError(
            f"deltas[{index}].evidence is not one exact contiguous quote "
            "from assistant_text"
        )
    subject = _normalize_item_v4_reference(
        raw.get("subject"),
        path=f"deltas[{index}].subject",
        allowed_kinds=_ADVANTAGE_V4_SUBJECT_KINDS[event_type],
        evidence=evidence,
    )
    objects = _normalize_advantage_v4_objects(
        raw.get("objects"),
        event_type=event_type,
        evidence=evidence,
        index=index,
    )
    roles = _advantage_v4_roles(objects)

    if event_type == "advantage_spec":
        if action == "supersede":
            _advantage_v4_require_roles(
                event_type=event_type,
                action=action,
                roles=roles,
                required=("supersedes_advantage",),
                index=index,
            )
    elif event_type == "advantage_anchor":
        required_roles = ["advantage"]
        if action == "define":
            required_roles.append("anchor_ref")
        _advantage_v4_require_roles(
            event_type=event_type,
            action=action,
            roles=roles,
            required=required_roles,
            index=index,
        )
    elif event_type == "advantage_module":
        _advantage_v4_require_roles(
            event_type=event_type,
            action=action,
            roles=roles,
            required=("advantage",),
            index=index,
        )
    elif event_type == "advantage_bind":
        required_roles = ["anchor"]
        if action == "bind":
            required_roles.append("owner")
        _advantage_v4_require_roles(
            event_type=event_type,
            action=action,
            roles=roles,
            required=required_roles,
            index=index,
        )
    elif event_type == "advantage_activate" and action == "activate":
        _advantage_v4_require_roles(
            event_type=event_type,
            action=action,
            roles=roles,
            required=("owner",),
            index=index,
        )
    elif event_type in {"advantage_trigger", "advantage_use"}:
        _advantage_v4_require_roles(
            event_type=event_type,
            action=action,
            roles=roles,
            required=("module",),
            index=index,
        )
    elif event_type == "advantage_reveal":
        required_roles = ["advantage"]
        if raw.get("knowledge_plane") == "actor_belief":
            required_roles.append("observer")
        _advantage_v4_require_roles(
            event_type=event_type,
            action=action,
            roles=roles,
            required=required_roles,
            index=index,
        )
    elif event_type == "advantage_contract":
        _advantage_v4_require_roles(
            event_type=event_type,
            action=action,
            roles=roles,
            required=("advantage",),
            index=index,
        )
        if subject["kind"] == "narrative_contract" and action != "narrative":
            raise StateRagError(
                f"deltas[{index}] narrative_contract requires action=narrative"
            )
        if subject["kind"] == "advantage_contract" and action == "narrative":
            raise StateRagError(
                f"deltas[{index}] action=narrative requires "
                "a narrative_contract subject"
            )
    elif event_type == "advantage_correction":
        _advantage_v4_require_roles(
            event_type=event_type,
            action=action,
            roles=roles,
            required=("target_event",),
            index=index,
        )

    scope = raw.get("scope")
    if not isinstance(scope, str) or scope not in _ITEM_V4_SCOPES:
        raise StateRagError(f"deltas[{index}].scope is invalid")
    if scope == "current" and _FUTURE_RE.search(evidence):
        scope = "planned"
    elif scope == "current" and _HISTORICAL_RE.search(evidence):
        scope = "historical"
    if scope == "timeless" and event_type not in {
        "advantage_spec",
        "advantage_anchor",
        "advantage_module",
    }:
        raise StateRagError(
            f"deltas[{index}].scope=timeless is reserved for "
            "Advantage definitions, anchors, and modules"
        )

    knowledge_plane = raw.get("knowledge_plane")
    if (
        not isinstance(knowledge_plane, str)
        or knowledge_plane not in POWER_KNOWLEDGE_PLANES
    ):
        raise StateRagError(
            f"deltas[{index}].knowledge_plane is invalid"
        )
    if knowledge_plane == "author_plan" and scope == "current":
        scope = "planned"
    if (
        event_type not in {"advantage_reveal", "advantage_correction"}
        and knowledge_plane
        in {"actor_belief", "public_narrative", "reader_disclosed"}
    ):
        raise StateRagError(
            f"deltas[{index}] {knowledge_plane} Advantage claims must use "
            "advantage_reveal instead of a state-changing Advantage event"
        )

    confidence_value = raw.get("confidence")
    if (
        isinstance(confidence_value, bool)
        or not isinstance(confidence_value, (int, float))
    ):
        raise StateRagError(
            f"deltas[{index}].confidence must be numeric"
        )
    confidence = float(confidence_value)
    if (
        not math.isfinite(confidence)
        or not float(min_confidence) <= confidence <= 1.0
    ):
        raise StateRagError(
            f"deltas[{index}].confidence is below {min_confidence} or above 1"
        )

    coordinate = _normalize_item_v4_coordinate(
        raw.get("story_coordinate"),
        index=index,
    )
    effective_at = raw.get("effective_at")
    if effective_at is not None:
        if isinstance(effective_at, bool) or not isinstance(
            effective_at, (str, int, float)
        ):
            raise StateRagError(
                f"deltas[{index}].effective_at must be a string or number"
            )
        if isinstance(effective_at, float) and not math.isfinite(effective_at):
            raise StateRagError(
                f"deltas[{index}].effective_at must be finite"
            )
        if isinstance(effective_at, str) and not effective_at.strip():
            raise StateRagError(
                f"deltas[{index}].effective_at must not be empty"
            )

    changes = _normalize_advantage_v4_changes(
        raw.get("changes"),
        event_type=event_type,
        action=action,
        subject_kind=subject["kind"],
        roles=roles,
        assistant_text=assistant_text,
        index=index,
        min_confidence=float(min_confidence),
    )
    ambiguity = raw.get("ambiguity")
    _validate_item_v4_json_tree(
        ambiguity,
        path=f"deltas[{index}].ambiguity",
    )
    normalized = {
        "schema_version": DELTA_V4_SCHEMA,
        "event_type": event_type,
        "action": action,
        "subject": subject,
        "objects": objects,
        "changes": changes,
        "scope": scope,
        "effective_at": effective_at,
        "story_coordinate": coordinate,
        "knowledge_plane": knowledge_plane,
        "ambiguity": ambiguity,
        "confidence": confidence,
        "evidence": evidence,
    }
    try:
        rendered = _json_dumps(normalized)
    except (TypeError, ValueError) as exc:
        raise StateRagError(
            f"deltas[{index}] contains non-strict JSON"
        ) from exc
    if len(rendered) > MAX_VALUE_JSON_CHARS:
        raise StateRagError(f"deltas[{index}] payload is too large")
    return normalized


def validate_delta_v4_envelope(
    extracted: Any,
    assistant_text: str,
    config: RuntimeConfig,
    prompt: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate a mixed v4 envelope without changing legacy v3 semantics."""

    if (
        not isinstance(extracted, Mapping)
        or set(extracted) != {"schema_version", "deltas"}
        or extracted.get("schema_version") != DELTA_V4_SCHEMA
    ):
        raise StateRagError(
            "typed extraction JSON must contain "
            "schema_version=plot-rag-delta/v4 and deltas"
        )
    raw_deltas = extracted.get("deltas")
    if not isinstance(raw_deltas, list) or len(raw_deltas) > 500:
        raise StateRagError(
            "extraction deltas must be an array with at most 500 items"
        )
    result: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    alternative_request = bool(
        _ALTERNATIVE_REQUEST_RE.search(prompt)
    ) and not bool(
        re.search(
            r"(?:不要|不需要|无需|拒绝).{0,8}(?:方案|备选|选项)",
            prompt,
        )
    )
    for index, raw in enumerate(raw_deltas):
        raw_event_type = (
            str(raw.get("event_type") or "").strip().casefold()
            if isinstance(raw, Mapping)
            else ""
        )
        if raw_event_type in ITEM_DELTA_EVENT_TYPES:
            normalized = normalize_item_extraction_candidate(
                raw,
                assistant_text,
                min_confidence=config.min_confidence,
                index=index,
            )
        elif raw_event_type in ADVANTAGE_DELTA_EVENT_TYPES:
            normalized = normalize_advantage_extraction_candidate(
                raw,
                assistant_text,
                min_confidence=config.min_confidence,
                index=index,
            )
        else:
            legacy, legacy_skipped = _validate_v3_deltas(
                {
                    "schema_version": DELTA_V3_SCHEMA,
                    "deltas": [raw],
                },
                assistant_text,
                config,
                prompt,
            )
            for value in legacy_skipped:
                adjusted = dict(value)
                adjusted["index"] = index
                skipped.append(adjusted)
            for normalized in legacy:
                fingerprint = _json_dumps(normalized)
                if fingerprint not in seen:
                    result.append(normalized)
                    seen.add(fingerprint)
            continue

        if raw_event_type in (
            ITEM_DELTA_EVENT_TYPES | ADVANTAGE_DELTA_EVENT_TYPES
        ):
            evidence = str(normalized["evidence"])
            evidence_offset = assistant_text.find(evidence)
            evidence_context = (
                assistant_text[
                    max(0, evidence_offset - 120) : min(
                        len(assistant_text),
                        evidence_offset + len(evidence) + 40,
                    )
                ]
                if evidence and evidence_offset >= 0
                else ""
            )
            if (
                alternative_request
                or _ALTERNATIVE_CONTEXT_RE.search(evidence_context)
            ):
                skipped.append(
                    {
                        "index": index,
                        "reason": "alternative_branch",
                        "evidence": evidence,
                    }
                )
                continue
            if _UNCERTAIN_RE.search(evidence):
                skipped.append(
                    {
                        "index": index,
                        "reason": "uncertain_or_conditional_branch",
                        "evidence": evidence,
                    }
                )
                continue
            fingerprint = _json_dumps(normalized)
            if fingerprint not in seen:
                result.append(normalized)
                seen.add(fingerprint)
            continue
    return result, skipped


def split_delta_v4_results(
    deltas: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Preserve the original legacy/item split for callers without Advantage."""

    legacy: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(deltas):
        if not isinstance(raw, Mapping):
            raise StateRagError(f"deltas[{index}] is not an object")
        value = dict(raw)
        event_type = str(value.get("event_type") or "").strip().casefold()
        schema_version = str(value.get("schema_version") or "")
        if event_type in ADVANTAGE_DELTA_EVENT_TYPES:
            if schema_version != DELTA_V4_SCHEMA:
                raise StateRagError(
                    f"deltas[{index}] Advantage candidate is not normalized v4"
                )
            raise StateRagError(
                "ADVANTAGE_DELTA_V1_REQUIRES_STRICT_PROPOSAL_ADAPTER"
            )
        if event_type in ITEM_DELTA_EVENT_TYPES:
            if schema_version != DELTA_V4_SCHEMA:
                raise StateRagError(
                    f"deltas[{index}] item candidate is not normalized v4"
                )
            items.append(value)
            continue
        if schema_version and schema_version != DELTA_V3_SCHEMA:
            raise StateRagError(
                f"deltas[{index}] legacy candidate is not normalized v3"
            )
        legacy.append(value)
    return legacy, items


def split_delta_v4_results_by_family(
    deltas: Sequence[Mapping[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Split normalized mixed results into legacy, Item, and Advantage."""

    legacy: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    advantages: list[dict[str, Any]] = []
    for index, raw in enumerate(deltas):
        if not isinstance(raw, Mapping):
            raise StateRagError(f"deltas[{index}] is not an object")
        value = dict(raw)
        event_type = str(value.get("event_type") or "").strip().casefold()
        schema_version = str(value.get("schema_version") or "")
        if event_type in ITEM_DELTA_EVENT_TYPES:
            if schema_version != DELTA_V4_SCHEMA:
                raise StateRagError(
                    f"deltas[{index}] item candidate is not normalized v4"
                )
            items.append(value)
            continue
        if event_type in ADVANTAGE_DELTA_EVENT_TYPES:
            if schema_version != DELTA_V4_SCHEMA:
                raise StateRagError(
                    f"deltas[{index}] Advantage candidate is not normalized v4"
                )
            advantages.append(value)
            continue
        # Older strict-runtime tests and compatibility callers may pass
        # already-validated legacy deltas without echoing schema_version.
        # Typed Item/Advantage candidates remain strict v4-only.
        if schema_version and schema_version != DELTA_V3_SCHEMA:
            raise StateRagError(
                f"deltas[{index}] legacy candidate is not normalized v3"
            )
        legacy.append(value)
    return legacy, items, advantages


def _raw_item_candidate_view(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    raw = {
        key: value
        for key, value in candidate.items()
        if key != "schema_version"
    }
    if (
        str(raw.get("event_type") or "").strip().casefold()
        == "item_correction"
        and isinstance(raw.get("changes"), Mapping)
    ):
        changes = dict(raw["changes"])
        if isinstance(changes.get("replacement"), Mapping):
            changes["replacement"] = _raw_item_candidate_view(
                changes["replacement"]
            )
        raw["changes"] = changes
    return raw


def _raw_advantage_candidate_view(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    raw = {
        key: value
        for key, value in candidate.items()
        if key != "schema_version"
    }
    if (
        str(raw.get("event_type") or "").strip().casefold()
        == "advantage_correction"
        and isinstance(raw.get("changes"), Mapping)
    ):
        changes = dict(raw["changes"])
        if isinstance(changes.get("replacement"), Mapping):
            changes["replacement"] = _raw_advantage_candidate_view(
                changes["replacement"]
            )
        raw["changes"] = changes
    return raw


def _item_adapter_issue(
    code: str,
    message: str,
    *,
    role: str,
    mention: str,
    reference_type: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "role": role,
        "mention": mention,
        "reference_type": reference_type,
    }
    payload.update(dict(details or {}))
    return {
        "code": code,
        "severity": "error",
        "message": message,
        "details": payload,
    }


def _resolved_reference_id(
    outcome: Any,
    *,
    mention: str,
    reference_type: str,
    role: str,
    issues: list[dict[str, Any]],
) -> str | None:
    if isinstance(outcome, str):
        value = outcome.strip()
        if value:
            return value
    if isinstance(outcome, Mapping):
        status = str(outcome.get("status") or "").strip().casefold()
        candidates = outcome.get("candidates")
        if status in {"ambiguous", "multiple"}:
            issues.append(
                _item_adapter_issue(
                    "ITEM_REFERENCE_AMBIGUOUS",
                    f"ambiguous item extraction reference: {mention}",
                    role=role,
                    mention=mention,
                    reference_type=reference_type,
                    details={
                        "candidates": (
                            list(candidates)
                            if isinstance(candidates, list)
                            else candidates
                        )
                    },
                )
            )
            return None
        if status in {"unresolved", "missing", "not_found", "not-found"}:
            issues.append(
                _item_adapter_issue(
                    "ITEM_REFERENCE_UNRESOLVED",
                    f"unresolved item extraction reference: {mention}",
                    role=role,
                    mention=mention,
                    reference_type=reference_type,
                    details={
                        "candidates": (
                            list(candidates)
                            if isinstance(candidates, list)
                            else candidates
                        )
                    },
                )
            )
            return None
        id_keys = (
            f"{reference_type}_id",
            "reference_id",
            "entity_id",
            "item_definition_id",
            "item_instance_id",
            "stack_id",
            "function_id",
            "binding_id",
            "spec_id",
            "event_id",
            "id",
        )
        for key in id_keys:
            value = outcome.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if isinstance(candidates, list) and len(candidates) > 1:
            issues.append(
                _item_adapter_issue(
                    "ITEM_REFERENCE_AMBIGUOUS",
                    f"ambiguous item extraction reference: {mention}",
                    role=role,
                    mention=mention,
                    reference_type=reference_type,
                    details={"candidates": list(candidates)},
                )
            )
            return None
    if isinstance(outcome, Sequence) and not isinstance(
        outcome, (str, bytes, bytearray)
    ):
        candidates = list(outcome)
        if len(candidates) == 1:
            return _resolved_reference_id(
                candidates[0],
                mention=mention,
                reference_type=reference_type,
                role=role,
                issues=issues,
            )
        if len(candidates) > 1:
            issues.append(
                _item_adapter_issue(
                    "ITEM_REFERENCE_AMBIGUOUS",
                    f"ambiguous item extraction reference: {mention}",
                    role=role,
                    mention=mention,
                    reference_type=reference_type,
                    details={"candidates": candidates},
                )
            )
            return None
    issues.append(
        _item_adapter_issue(
            "ITEM_REFERENCE_UNRESOLVED",
            f"unresolved item extraction reference: {mention}",
            role=role,
            mention=mention,
            reference_type=reference_type,
        )
    )
    return None


def _resolve_item_adapter_reference(
    resolver: Callable[[str, str, str], Any],
    *,
    mention: str,
    reference_type: str,
    role: str,
    issues: list[dict[str, Any]],
) -> str | None:
    try:
        outcome = resolver(mention, reference_type, role)
    except Exception as exc:
        issues.append(
            _item_adapter_issue(
                "ITEM_REFERENCE_RESOLVER_ERROR",
                f"item reference resolver failed for {mention}",
                role=role,
                mention=mention,
                reference_type=reference_type,
                details={"error": str(exc)},
            )
        )
        return None
    return _resolved_reference_id(
        outcome,
        mention=mention,
        reference_type=reference_type,
        role=role,
        issues=issues,
    )


def _item_adapter_reference_type_for_role(role: str) -> str:
    if role in {"item_definition"}:
        return "item_definition"
    if role in {"item_entity"}:
        return "item"
    if role in {"item_instance", "from_container", "to_container"}:
        return "item_instance"
    if role in {"source_stack", "target_stack", "item_stack"}:
        return "item_stack"
    if role in {"function"}:
        return "item_function"
    if role in {"ability"}:
        return "ability"
    if role in {"supersedes_spec"}:
        return "item_spec"
    if role in {"target_event"}:
        return "item_event"
    if role in {"from_location", "to_location", "location"}:
        return "location"
    if role in {"resource"}:
        return "resource"
    if role in {"item"}:
        return "item_subject"
    return "entity"


def _item_adapter_subject_reference_type(subject_kind: str) -> str:
    return {
        "item_definition": "item_definition",
        "function_definition": "item_function",
        "function_binding": "item_function_binding",
        "item_instance": "item_instance",
        "item_stack": "item_stack",
        "item_event": "item_event",
    }[subject_kind]


def _item_adapter_evidence(
    candidate: Mapping[str, Any],
    artifact_context: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "quote": str(candidate["evidence"]),
        "source": "assistant_text",
        "confidence": float(candidate["confidence"]),
    }
    for key in (
        "receipt_id",
        "assistant_sha256",
        "artifact_id",
        "source_path",
        "source_sha256",
    ):
        value = artifact_context.get(key)
        if value is not None and value != "":
            evidence[key] = value
    if metadata:
        evidence["explicit_metadata"] = dict(metadata)
    return evidence


def _validate_adapted_item_event(
    event: Mapping[str, Any],
    artifact_context: Mapping[str, Any],
    issues: list[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        try:
            from .continuity.validators import (
                ContinuityError,
                normalize_event,
            )
        except ImportError:
            from continuity.validators import (
                ContinuityError,
                normalize_event,
            )
        return normalize_event(
            event,
            artifact_stage=str(
                artifact_context.get("artifact_stage") or "draft"
            ),
            branch_id=str(artifact_context.get("branch_id") or "main"),
            chapter_no=artifact_context.get("chapter_no"),
            scene_index=artifact_context.get("scene_index"),
        )
    except ContinuityError as exc:
        issues.append(
            {
                "code": str(exc.code),
                "severity": "error",
                "message": str(exc.message),
                "details": {
                    **dict(exc.details),
                    "adapter_stage": "continuity_validator",
                },
            }
        )
        return None


def adapt_item_extraction_candidate(
    candidate: Mapping[str, Any],
    assistant_text: str,
    artifact_context: Mapping[str, Any],
    resolver: Callable[[str, str, str], Any],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Convert one neutral v4 candidate into a typed continuity item event.

    ``resolver`` is injected by the strict runtime and is called as
    ``resolver(mention, reference_type, role)``.  It may return a stable ID
    string or a mapping such as ``{"status": "RESOLVED", "entity_id": ...}``.
    Ambiguous, missing, or failing resolutions return structured issues and no
    event.  This adapter never reads or computes item before/after state.
    """

    if not isinstance(candidate, Mapping):
        raise StateRagError("item candidate must be an object")
    if (
        "schema_version" in candidate
        and candidate.get("schema_version") != DELTA_V4_SCHEMA
    ):
        raise StateRagError(
            f"deltas[0].schema_version must be {DELTA_V4_SCHEMA}"
        )
    try:
        normalized = normalize_item_extraction_candidate(
            candidate,
            assistant_text,
            min_confidence=min_confidence,
        )
    except StateRagError as exc:
        message = str(exc)
        code = "ITEM_EXTRACTION_CANDIDATE_INVALID"
        if "ability_bridge requires an explicit granted ability" in message:
            code = "ITEM_ABILITY_BRIDGE_REQUIRED"
        elif (
            "inline item functions cannot also grant bridged abilities"
            in message
            or "ability_bridge effects/costs/cooldown belong" in message
        ):
            code = "ITEM_ABILITY_BRIDGE_DUPLICATE"
        elif "requires one explicit destination custody anchor" in message:
            code = "ITEM_CUSTODY_ANCHOR_REQUIRED"
        return {
            "ok": False,
            "event": None,
            "issues": [
                {
                    "code": code,
                    "severity": "error",
                    "message": message,
                    "details": {
                        "adapter_stage": "neutral_candidate_validator",
                    },
                }
            ],
        }
    issues: list[dict[str, Any]] = []
    ambiguity = normalized.get("ambiguity")
    if (
        ambiguity is not None
        and ambiguity != ""
        and ambiguity != []
        and ambiguity != {}
    ):
        issues.append(
            {
                "code": "ITEM_EXTRACTION_AMBIGUITY",
                "severity": "error",
                "message": "item extraction candidate reports unresolved ambiguity",
                "details": {"ambiguity": ambiguity},
            }
        )
        return {"ok": False, "event": None, "issues": issues}

    subject = normalized["subject"]
    subject_kind = str(subject["kind"])
    subject_mention = str(subject["mention"])
    subject_id = _resolve_item_adapter_reference(
        resolver,
        mention=subject_mention,
        reference_type=_item_adapter_subject_reference_type(subject_kind),
        role="subject",
        issues=issues,
    )
    resolved_objects: dict[str, str] = {}
    for reference in normalized["objects"]:
        role = str(reference["role"])
        mention = str(reference["mention"])
        if role == "slot":
            resolved_objects[role] = mention
            continue
        resolved = _resolve_item_adapter_reference(
            resolver,
            mention=mention,
            reference_type=_item_adapter_reference_type_for_role(role),
            role=role,
            issues=issues,
        )
        if resolved is not None:
            resolved_objects[role] = resolved
    if any(issue.get("severity") == "error" for issue in issues):
        return {"ok": False, "event": None, "issues": issues}
    if subject_id is None:  # Defensive: every resolver miss must carry an issue.
        issues.append(
            _item_adapter_issue(
                "ITEM_REFERENCE_UNRESOLVED",
                f"unresolved item extraction subject: {subject_mention}",
                role="subject",
                mention=subject_mention,
                reference_type=_item_adapter_subject_reference_type(
                    subject_kind
                ),
            )
        )
        return {"ok": False, "event": None, "issues": issues}

    context = dict(artifact_context or {})
    event: dict[str, Any] = {
        "schema_version": DELTA_V4_SCHEMA,
        "event_type": normalized["event_type"],
        "action": normalized["action"],
        "scope": normalized["scope"],
        "branch_id": str(context.get("branch_id") or "main"),
        "chapter_no": context.get("chapter_no"),
        "scene_index": context.get("scene_index"),
        "story_time": (
            context.get("story_time")
            if context.get("story_time") is not None
            else normalized.get("effective_at")
        ),
        "story_coordinate": dict(normalized["story_coordinate"]),
        "narrative_mode": str(
            context.get("narrative_mode") or "linear"
        ),
        "knowledge_plane": normalized["knowledge_plane"],
        "confidence": normalized["confidence"],
        "effective_at": normalized.get("effective_at"),
        "ambiguity": normalized.get("ambiguity"),
    }
    changes = dict(normalized["changes"])
    metadata = {
        key: changes[key]
        for key in ("reason", "terms", "observed_effects")
        if key in changes
    }
    event["evidence"] = _item_adapter_evidence(
        normalized,
        context,
        metadata=metadata,
    )
    event_type = str(normalized["event_type"])
    action = str(normalized["action"])

    if event_type == "item_spec":
        spec_type = subject_kind
        id_field = {
            "item_definition": "item_definition_id",
            "function_definition": "function_id",
            "function_binding": "binding_id",
        }[spec_type]
        event.update(
            {
                "spec_type": spec_type,
                "spec_id": subject_id,
                id_field: subject_id,
            }
        )
        definition = dict(changes.get("definition") or {})
        if spec_type == "item_definition":
            function_mentions = definition.pop("default_functions", [])
            if function_mentions:
                function_ids: list[str] = []
                for mention in function_mentions:
                    resolved = _resolve_item_adapter_reference(
                        resolver,
                        mention=str(mention),
                        reference_type="item_function",
                        role="definition.default_functions",
                        issues=issues,
                    )
                    if resolved is not None:
                        function_ids.append(resolved)
                definition["default_functions"] = function_ids
        elif spec_type == "function_definition":
            definition["item_definition_id"] = resolved_objects[
                "item_definition"
            ]
            ability_mentions = definition.pop("granted_abilities", [])
            if ability_mentions:
                ability_ids: list[str] = []
                for mention in ability_mentions:
                    resolved = _resolve_item_adapter_reference(
                        resolver,
                        mention=str(mention),
                        reference_type="ability",
                        role="definition.granted_abilities",
                        issues=issues,
                    )
                    if resolved is not None:
                        ability_ids.append(resolved)
                definition["granted_ability_ids"] = ability_ids
            elif "ability" in resolved_objects:
                definition["granted_ability_ids"] = [
                    resolved_objects["ability"]
                ]
        else:
            definition["function_id"] = resolved_objects["function"]
            if "item_definition" in resolved_objects:
                definition["item_definition_id"] = resolved_objects[
                    "item_definition"
                ]
            elif "item_instance" in resolved_objects:
                definition["item_instance_id"] = resolved_objects[
                    "item_instance"
                ]
            else:
                definition["stack_id"] = resolved_objects["item_stack"]
        event["definition"] = definition
        if action == "supersede":
            event["supersedes_spec_id"] = resolved_objects[
                "supersedes_spec"
            ]

    elif event_type == "item_instance":
        event.update(
            {
                "subject_type": subject_kind,
                "subject_id": subject_id,
                (
                    "item_instance_id"
                    if subject_kind == "item_instance"
                    else "stack_id"
                ): subject_id,
            }
        )
        if "item_definition" in resolved_objects:
            event["item_definition_id"] = resolved_objects[
                "item_definition"
            ]
        if "item_entity" in resolved_objects:
            event["item_entity_id"] = resolved_objects["item_entity"]
        if "source_stack" in resolved_objects:
            event["source_stack_id"] = resolved_objects["source_stack"]
        if "target_stack" in resolved_objects:
            event["target_stack_id"] = resolved_objects["target_stack"]
        for key in (
            "quantity",
            "batch",
            "target_batch",
            "attributes",
            "instance_name",
            "serial_or_mark",
            "unique",
            "provenance",
        ):
            if key in changes:
                event[key] = changes[key]

    elif event_type == "item_custody":
        event.update(
            {
                "subject_type": subject_kind,
                "subject_id": subject_id,
                (
                    "item_instance_id"
                    if subject_kind == "item_instance"
                    else "stack_id"
                ): subject_id,
            }
        )
        custody_fields = {
            "actor": "actor_entity_id",
            "from_legal_owner": "from_legal_owner_entity_id",
            "to_legal_owner": "to_legal_owner_entity_id",
            "from_carrier": "from_carrier_entity_id",
            "to_carrier": "to_carrier_entity_id",
            "from_custodian": "from_custodian_entity_id",
            "to_custodian": "to_custodian_entity_id",
            "from_access_controller": (
                "from_access_controller_entity_id"
            ),
            "to_access_controller": "to_access_controller_entity_id",
            "from_container": "from_container_instance_id",
            "to_container": "to_container_instance_id",
            "from_location": "from_location_entity_id",
            "to_location": "to_location_entity_id",
        }
        for role, field_name in custody_fields.items():
            if role in resolved_objects:
                event[field_name] = resolved_objects[role]
        if "quantity" in changes:
            event["quantity"] = changes["quantity"]
        if "custody_status" in changes:
            event["custody_status"] = changes["custody_status"]

    elif event_type == "item_runtime":
        event.update(
            {
                "subject_type": "item_instance",
                "subject_id": subject_id,
                "item_instance_id": subject_id,
            }
        )
        if "delta" in changes:
            event["delta"] = dict(changes["delta"])
        if "actor" in resolved_objects:
            event["actor_entity_id"] = resolved_objects["actor"]
        if "function" in resolved_objects:
            event["function_id"] = resolved_objects["function"]
        if "target" in resolved_objects:
            event["target_entity_id"] = resolved_objects["target"]
        if "equipped_by" in resolved_objects:
            event["equipped_by_entity_id"] = resolved_objects["equipped_by"]
        if "bound_actor" in resolved_objects:
            event["bound_actor_entity_id"] = resolved_objects["bound_actor"]
        slot_key = changes.get("slot_key") or resolved_objects.get("slot")
        if slot_key:
            event["slot_key"] = str(slot_key)
        for key in (
            "durability",
            "max_durability",
            "energy",
            "max_energy",
            "sealed",
            "damaged",
            "destroyed",
            "active",
            "state",
        ):
            if key in changes:
                event[key] = changes[key]

    elif event_type == "item_function_runtime":
        event.update(
            {
                "subject_type": subject_kind,
                "subject_id": subject_id,
                (
                    "item_instance_id"
                    if subject_kind == "item_instance"
                    else "stack_id"
                ): subject_id,
                "function_id": resolved_objects["function"],
            }
        )
        if "delta" in changes:
            event["delta"] = dict(changes["delta"])
        for key in (
            "enabled",
            "unlock_state",
            "remaining_charges",
            "cooldown_until",
            "state",
            "reason",
        ):
            if key in changes:
                event[key] = changes[key]

    elif event_type == "item_use":
        event.update(
            {
                "subject_type": subject_kind,
                "subject_id": subject_id,
                (
                    "item_instance_id"
                    if subject_kind == "item_instance"
                    else "stack_id"
                ): subject_id,
                "actor_entity_id": resolved_objects["actor"],
                "function_id": resolved_objects["function"],
                "delta": dict(changes.get("delta") or {}),
            }
        )
        if "target" in resolved_objects:
            event["target_entity_id"] = resolved_objects["target"]
        if "location" in resolved_objects:
            event["location_entity_id"] = resolved_objects["location"]
        if "resource" in resolved_objects:
            event["resource_entity_id"] = resolved_objects["resource"]

    elif event_type == "item_observation":
        subject_id_field = {
            "item_definition": "item_definition_id",
            "item_instance": "item_instance_id",
            "item_stack": "stack_id",
        }[subject_kind]
        event.update(
            {
                "subject_type": subject_kind,
                "subject_id": subject_id,
                subject_id_field: subject_id,
                "observation": dict(changes["observation"]),
            }
        )
        if "observer" in resolved_objects:
            event["observer_entity_id"] = resolved_objects["observer"]
        if "function" in resolved_objects:
            event["function_id"] = resolved_objects["function"]
        if "observed_actor" in resolved_objects:
            event["target_entity_id"] = resolved_objects["observed_actor"]
        if "source" in resolved_objects:
            event["source_entity_id"] = resolved_objects["source"]

    elif event_type == "item_correction":
        target_event_id = resolved_objects["target_event"]
        if target_event_id != subject_id:
            issues.append(
                {
                    "code": "ITEM_CORRECTION_TARGET_MISMATCH",
                    "severity": "error",
                    "message": "item correction subject and target_event differ",
                    "details": {
                        "subject_event_id": subject_id,
                        "target_event_id": target_event_id,
                    },
                }
            )
            return {"ok": False, "event": None, "issues": issues}
        event["target_event_id"] = target_event_id
        if action != "retract":
            replacement_result = adapt_item_extraction_candidate(
                changes["replacement"],
                assistant_text,
                artifact_context,
                resolver,
                min_confidence=min_confidence,
            )
            issues.extend(replacement_result["issues"])
            if replacement_result["event"] is None:
                return {"ok": False, "event": None, "issues": issues}
            event["replacement"] = replacement_result["event"]

    if any(issue.get("severity") == "error" for issue in issues):
        return {"ok": False, "event": None, "issues": issues}
    validated_event = _validate_adapted_item_event(
        event,
        context,
        issues,
    )
    if validated_event is None:
        return {"ok": False, "event": None, "issues": issues}
    return {"ok": True, "event": validated_event, "issues": issues}


def adapt_item_extraction_candidates(
    candidates: Sequence[Mapping[str, Any]],
    assistant_text: str,
    artifact_context: Mapping[str, Any],
    resolver: Callable[[str, str, str], Any],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Adapt one ordered batch of neutral v4 item candidates for proposal use.

    Per-candidate validation and resolution failures are returned as structured
    issues while independent valid candidates remain available to the Stop
    proposal path.  Invalid top-level invocation contracts still fail closed
    with :class:`StateRagError`.
    """

    if (
        not isinstance(candidates, Sequence)
        or isinstance(candidates, (str, bytes, bytearray))
    ):
        raise StateRagError("item candidates must be an array")
    if not isinstance(artifact_context, Mapping):
        raise StateRagError("artifact_context must be an object")
    if not callable(resolver):
        raise StateRagError("item reference resolver must be callable")

    events: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            issues.append(
                {
                    "code": "ITEM_EXTRACTION_CANDIDATE_INVALID",
                    "severity": "error",
                    "message": f"item candidate {index} must be an object",
                    "details": {
                        "candidate_index": index,
                        "adapter_stage": "batch_contract",
                    },
                }
            )
            continue
        try:
            adapted = adapt_item_extraction_candidate(
                candidate,
                assistant_text,
                artifact_context,
                resolver,
                min_confidence=min_confidence,
            )
        except StateRagError as exc:
            issues.append(
                {
                    "code": "ITEM_EXTRACTION_CANDIDATE_INVALID",
                    "severity": "error",
                    "message": str(exc),
                    "details": {
                        "candidate_index": index,
                        "adapter_stage": "candidate_contract",
                    },
                }
            )
            continue
        for raw_issue in adapted.get("issues") or []:
            issue = dict(raw_issue)
            details = dict(issue.get("details") or {})
            details.setdefault("candidate_index", index)
            issue["details"] = details
            issues.append(issue)
        event = adapted.get("event")
        if isinstance(event, Mapping):
            events.append(dict(event))

    return {
        "ok": not any(
            str(issue.get("severity") or "error").casefold()
            in {"error", "critical"}
            for issue in issues
        ),
        "events": events,
        "issues": issues,
        "candidate_count": len(candidates),
        "adapted_count": len(events),
    }


def _advantage_adapter_issue(
    code: str,
    message: str,
    *,
    role: str = "",
    mention: str = "",
    reference_type: str = "",
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = dict(details or {})
    if role:
        payload.setdefault("role", role)
    if mention:
        payload.setdefault("mention", mention)
    if reference_type:
        payload.setdefault("reference_type", reference_type)
    return {
        "code": code,
        "severity": "error",
        "message": message,
        "details": payload,
    }


def _resolved_advantage_reference(
    outcome: Any,
    *,
    mention: str,
    reference_type: str,
    role: str,
    issues: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any]]:
    metadata: dict[str, Any] = (
        dict(outcome) if isinstance(outcome, Mapping) else {}
    )
    if isinstance(outcome, str):
        value = outcome.strip()
        if value:
            return value, metadata
    if isinstance(outcome, Mapping):
        status = str(outcome.get("status") or "").strip().casefold()
        candidates = outcome.get("candidates")
        if status in {"ambiguous", "multiple"}:
            issues.append(
                _advantage_adapter_issue(
                    "ADVANTAGE_REFERENCE_AMBIGUOUS",
                    f"ambiguous Advantage extraction reference: {mention}",
                    role=role,
                    mention=mention,
                    reference_type=reference_type,
                    details={
                        "candidates": (
                            list(candidates)
                            if isinstance(candidates, list)
                            else candidates
                        )
                    },
                )
            )
            return None, metadata
        if status in {"unresolved", "missing", "not_found", "not-found"}:
            issues.append(
                _advantage_adapter_issue(
                    "ADVANTAGE_REFERENCE_UNRESOLVED",
                    f"unresolved Advantage extraction reference: {mention}",
                    role=role,
                    mention=mention,
                    reference_type=reference_type,
                    details={
                        "candidates": (
                            list(candidates)
                            if isinstance(candidates, list)
                            else candidates
                        )
                    },
                )
            )
            return None, metadata
        id_keys = (
            f"{reference_type}_id",
            "reference_id",
            "advantage_id",
            "anchor_id",
            "module_id",
            "knowledge_id",
            "contract_id",
            "narrative_contract_id",
            "event_id",
            "entity_id",
            "ability_id",
            "item_instance_id",
            "stack_id",
            "id",
        )
        for key in id_keys:
            value = outcome.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), metadata
        if isinstance(candidates, list) and len(candidates) > 1:
            issues.append(
                _advantage_adapter_issue(
                    "ADVANTAGE_REFERENCE_AMBIGUOUS",
                    f"ambiguous Advantage extraction reference: {mention}",
                    role=role,
                    mention=mention,
                    reference_type=reference_type,
                    details={"candidates": list(candidates)},
                )
            )
            return None, metadata
    if isinstance(outcome, Sequence) and not isinstance(
        outcome, (str, bytes, bytearray)
    ):
        candidates = list(outcome)
        if len(candidates) == 1:
            return _resolved_advantage_reference(
                candidates[0],
                mention=mention,
                reference_type=reference_type,
                role=role,
                issues=issues,
            )
        if len(candidates) > 1:
            issues.append(
                _advantage_adapter_issue(
                    "ADVANTAGE_REFERENCE_AMBIGUOUS",
                    f"ambiguous Advantage extraction reference: {mention}",
                    role=role,
                    mention=mention,
                    reference_type=reference_type,
                    details={"candidates": candidates},
                )
            )
            return None, metadata
    issues.append(
        _advantage_adapter_issue(
            "ADVANTAGE_REFERENCE_UNRESOLVED",
            f"unresolved Advantage extraction reference: {mention}",
            role=role,
            mention=mention,
            reference_type=reference_type,
        )
    )
    return None, metadata


def _resolve_advantage_adapter_reference(
    resolver: Callable[[str, str, str], Any],
    *,
    mention: str,
    reference_type: str,
    role: str,
    issues: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any]]:
    try:
        outcome = resolver(mention, reference_type, role)
    except Exception as exc:
        issues.append(
            _advantage_adapter_issue(
                "ADVANTAGE_REFERENCE_RESOLVER_ERROR",
                f"Advantage reference resolver failed for {mention}",
                role=role,
                mention=mention,
                reference_type=reference_type,
                details={"error": str(exc)},
            )
        )
        return None, {}
    return _resolved_advantage_reference(
        outcome,
        mention=mention,
        reference_type=reference_type,
        role=role,
        issues=issues,
    )


def _advantage_adapter_reference_type_for_role(
    role: str,
    *,
    anchor_type: str | None = None,
) -> str:
    if role == "advantage":
        return "advantage"
    if role == "anchor":
        return "advantage_anchor"
    if role == "anchor_ref":
        mapped = {
            "item_instance": "item_instance",
            "item_stack": "item_stack",
            "body_or_vessel": "entity",
            "actor": "entity",
            "location": "location",
        }.get(str(anchor_type or "").casefold())
        return mapped or "anchor_ref"
    if role in {"owner", "actor", "target", "observer", "counterparty"}:
        return "entity"
    if role in {"module", "unlock_module"}:
        return "advantage_module"
    if role == "granted_ability":
        return "ability"
    if role in {"caused_by", "target_event"}:
        return "advantage_event"
    if role == "misread_of":
        return "advantage_knowledge"
    if role == "supersedes_advantage":
        return "advantage"
    return "entity"


def _advantage_adapter_subject_reference_type(subject_kind: str) -> str:
    return {
        "advantage_definition": "advantage",
        "advantage_anchor": "advantage_anchor",
        "advantage_module": "advantage_module",
        "advantage": "advantage",
        "advantage_knowledge": "advantage_knowledge",
        "advantage_contract": "advantage_contract",
        "narrative_contract": "narrative_contract",
        "advantage_event": "advantage_event",
    }[subject_kind]


def _advantage_experience_required(context: Mapping[str, Any]) -> bool:
    if bool(context.get("advantage_experience_required")):
        return True
    lifecycle = context.get("lifecycle_identity")
    if isinstance(lifecycle, Mapping) and lifecycle:
        return True
    return bool(
        context.get("event_seed_manifest_hash")
        or context.get("experience_contract_hashes")
    )


def _validate_adapted_advantage_event(
    event: Mapping[str, Any],
    artifact_context: Mapping[str, Any],
    issues: list[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        try:
            from .continuity.validators import (
                ContinuityError,
                normalize_event,
                validate_advantage_experience_contract_bindings,
            )
        except ImportError:
            from continuity.validators import (
                ContinuityError,
                normalize_event,
                validate_advantage_experience_contract_bindings,
            )
        normalized = normalize_event(
            event,
            artifact_stage=str(
                artifact_context.get("artifact_stage") or "draft"
            ),
            branch_id=str(artifact_context.get("branch_id") or "main"),
            chapter_no=artifact_context.get("chapter_no"),
            scene_index=artifact_context.get("scene_index"),
        )
        if _advantage_experience_required(artifact_context):
            allowed_ids = artifact_context.get(
                "advantage_allowed_experience_contract_ids"
            )
            local_binding = {
                "contract_id": artifact_context.get(
                    "experience_contract_id"
                ),
                "contract_hash": artifact_context.get(
                    "experience_contract_hash"
                ),
                "event_seed_id": artifact_context.get("event_seed_id"),
                "event_seed_revision": artifact_context.get(
                    "event_seed_revision"
                ),
            }
            validate_advantage_experience_contract_bindings(
                [normalized],
                required=True,
                allowed_contract_ids=(
                    list(allowed_ids)
                    if isinstance(allowed_ids, Sequence)
                    and not isinstance(allowed_ids, (str, bytes, bytearray))
                    else None
                ),
                allowed_contract_bindings=(
                    [local_binding]
                    if local_binding["contract_id"] not in (None, "")
                    else None
                ),
            )
        return normalized
    except ContinuityError as exc:
        issues.append(
            {
                "code": str(exc.code),
                "severity": "error",
                "message": str(exc.message),
                "details": {
                    **dict(exc.details),
                    "adapter_stage": "continuity_validator",
                },
            }
        )
        return None


def adapt_advantage_extraction_candidate(
    candidate: Mapping[str, Any],
    assistant_text: str,
    artifact_context: Mapping[str, Any],
    resolver: Callable[[str, str, str], Any],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Convert one neutral Advantage candidate into a typed local event."""

    if not isinstance(candidate, Mapping):
        raise StateRagError("Advantage candidate must be an object")
    if (
        "schema_version" in candidate
        and candidate.get("schema_version") != DELTA_V4_SCHEMA
    ):
        raise StateRagError(
            f"deltas[0].schema_version must be {DELTA_V4_SCHEMA}"
        )
    if not isinstance(artifact_context, Mapping):
        raise StateRagError("artifact_context must be an object")
    if not callable(resolver):
        raise StateRagError("Advantage reference resolver must be callable")

    try:
        normalized = normalize_advantage_extraction_candidate(
            candidate,
            assistant_text,
            min_confidence=min_confidence,
        )
    except StateRagError as exc:
        message = str(exc)
        code = "ADVANTAGE_EXTRACTION_CANDIDATE_INVALID"
        if "remote computed/stable-id field" in message:
            code = "ADVANTAGE_COMPUTED_STATE_FORBIDDEN"
        elif "subject.kind is unsupported" in message:
            code = "ADVANTAGE_SUBJECT_KIND_INVALID"
        elif ".role is unsupported" in message:
            code = "ADVANTAGE_OBJECT_ROLE_INVALID"
        elif "requires object roles" in message:
            code = "ADVANTAGE_REQUIRED_ROLE_MISSING"
        elif "story_coordinate" in message:
            code = "ADVANTAGE_STORY_COORDINATE_REQUIRED"
        elif "correction" in message and "target" in message:
            code = "ADVANTAGE_CORRECTION_TARGET_MISMATCH"
        return {
            "ok": False,
            "event": None,
            "issues": [
                {
                    "code": code,
                    "severity": "error",
                    "message": message,
                    "details": {"adapter_stage": "neutral_candidate_validator"},
                }
            ],
        }

    issues: list[dict[str, Any]] = []
    ambiguity = normalized.get("ambiguity")
    if ambiguity not in (None, "", [], {}):
        issues.append(
            {
                "code": "ADVANTAGE_EXTRACTION_AMBIGUITY",
                "severity": "error",
                "message": (
                    "Advantage extraction candidate reports unresolved ambiguity"
                ),
                "details": {"ambiguity": ambiguity},
            }
        )
        return {"ok": False, "event": None, "issues": issues}

    context = dict(artifact_context or {})
    subject = normalized["subject"]
    subject_kind = str(subject["kind"])
    subject_mention = str(subject["mention"])
    subject_id, subject_metadata = _resolve_advantage_adapter_reference(
        resolver,
        mention=subject_mention,
        reference_type=_advantage_adapter_subject_reference_type(
            subject_kind
        ),
        role="subject",
        issues=issues,
    )
    roles = _advantage_v4_roles(normalized["objects"])
    changes = dict(normalized["changes"])
    anchor_type = changes.get("anchor_type")
    resolved_objects: dict[str, list[str]] = {}
    object_metadata: dict[str, list[dict[str, Any]]] = {}
    for reference in normalized["objects"]:
        role = str(reference["role"])
        mention = str(reference["mention"])
        reference_type = _advantage_adapter_reference_type_for_role(
            role,
            anchor_type=(
                str(anchor_type) if anchor_type is not None else None
            ),
        )
        resolved, metadata = _resolve_advantage_adapter_reference(
            resolver,
            mention=mention,
            reference_type=reference_type,
            role=role,
            issues=issues,
        )
        if resolved is not None:
            resolved_objects.setdefault(role, []).append(resolved)
            object_metadata.setdefault(role, []).append(metadata)

    if any(issue.get("severity") == "error" for issue in issues):
        return {"ok": False, "event": None, "issues": issues}
    if subject_id is None:
        issues.append(
            _advantage_adapter_issue(
                "ADVANTAGE_REFERENCE_UNRESOLVED",
                f"unresolved Advantage extraction subject: {subject_mention}",
                role="subject",
                mention=subject_mention,
                reference_type=_advantage_adapter_subject_reference_type(
                    subject_kind
                ),
            )
        )
        return {"ok": False, "event": None, "issues": issues}

    event_type = str(normalized["event_type"])
    action = str(normalized["action"])
    event: dict[str, Any] = {
        "schema_version": ADVANTAGE_EVENT_SCHEMA,
        "event_type": event_type,
        "scope": normalized["scope"],
        "branch_id": str(context.get("branch_id") or "main"),
        "chapter_no": context.get("chapter_no"),
        "scene_index": context.get("scene_index"),
        "story_time": (
            context.get("story_time")
            if context.get("story_time") is not None
            else normalized.get("effective_at")
        ),
        "narrative_mode": str(context.get("narrative_mode") or "linear"),
        "story_coordinate": dict(normalized["story_coordinate"]),
        "knowledge_plane": normalized["knowledge_plane"],
        "confidence": normalized["confidence"],
        "effective_at": normalized.get("effective_at"),
        "ambiguity": normalized.get("ambiguity"),
        "advantage_id": None,
    }
    event["evidence"] = _item_adapter_evidence(normalized, context)

    # Local lifecycle binding is copied from the runtime context, never from
    # the remote candidate.
    for key in (
        "experience_contract_id",
        "experience_contract",
        "experience_contract_hash",
    ):
        value = context.get(key)
        if value not in (None, ""):
            event[key] = value
    provenance = context.get("causal_provenance")
    if isinstance(provenance, Mapping):
        event["causal_provenance"] = dict(provenance)
    seed_id = context.get("event_seed_id")
    seed_revision = context.get("event_seed_revision")
    if seed_id not in (None, ""):
        event.setdefault("causal_provenance", {})["event_seed_id"] = seed_id
    if seed_revision is not None:
        event.setdefault("causal_provenance", {})[
            "event_seed_revision"
        ] = seed_revision

    if event_type == "advantage_spec":
        event.update(
            {
                "action": action,
                "spec_type": "advantage_definition",
                "spec_id": subject_id,
                "advantage_id": subject_id,
            }
        )
        if "title" in changes or action == "define":
            event["title"] = changes.get("title") or subject_mention
        if "anchor_type" in changes:
            event["anchor_type"] = changes["anchor_type"]
        for key in (
            "profiles",
            "acquisition_mode",
            "uniqueness",
            "promise",
            "counterplay",
            "definition",
        ):
            if key in changes:
                event[key] = changes[key]
        if "supersedes_advantage" in resolved_objects:
            event["supersedes"] = list(
                resolved_objects["supersedes_advantage"]
            )

    elif event_type == "advantage_anchor":
        event.update(
            {
                "action": action,
                "advantage_id": resolved_objects["advantage"][0],
                "anchor_id": subject_id,
                "anchor_ref_id": resolved_objects["anchor_ref"][0]
                if resolved_objects.get("anchor_ref")
                else None,
                "anchor_type": changes.get("anchor_type"),
                "anchor_name": subject_mention,
            }
        )
        if resolved_objects.get("owner"):
            event["owner_entity_id"] = resolved_objects["owner"][0]
        for key in ("binding_state", "transfer_rule", "attributes"):
            if key in changes:
                event[key] = changes[key]

    elif event_type == "advantage_module":
        event.update(
            {
                "action": action,
                "advantage_id": resolved_objects["advantage"][0],
                "module_id": subject_id,
            }
        )
        if "title" in changes or action == "define":
            event["title"] = changes.get("title") or subject_mention
        if "kind" in changes:
            event["kind"] = changes["kind"]
        if resolved_objects.get("anchor"):
            event["anchor_ids"] = list(resolved_objects["anchor"])
        if resolved_objects.get("granted_ability"):
            event["granted_ability_ids"] = list(
                resolved_objects["granted_ability"]
            )
        for key in (
            "module_status",
            "stage",
            "trigger",
            "preconditions",
            "targets",
            "costs",
            "effects",
            "side_effects",
            "failure_modes",
            "counters",
        ):
            if key in changes:
                event[key] = changes[key]

    elif event_type == "advantage_bind":
        event.update(
            {
                "action": action,
                "advantage_id": subject_id,
                "anchor_id": resolved_objects["anchor"][0],
            }
        )
        if resolved_objects.get("owner"):
            event["owner_entity_id"] = resolved_objects["owner"][0]

    elif event_type == "advantage_activate":
        event.update(
            {
                "action": action,
                "advantage_id": subject_id,
            }
        )
        if resolved_objects.get("owner"):
            event["owner_entity_id"] = resolved_objects["owner"][0]
        if "stage" in changes:
            event["stage"] = changes["stage"]

    elif event_type in {
        "advantage_trigger",
        "advantage_use",
        "advantage_reward",
        "advantage_cost",
    }:
        event["advantage_id"] = subject_id
        if resolved_objects.get("module"):
            event["module_id"] = resolved_objects["module"][0]
        for role, field in (
            ("actor", "actor_entity_id"),
            ("target", "target_entity_id"),
        ):
            if resolved_objects.get(role):
                event[field] = resolved_objects[role][0]
        if resolved_objects.get("caused_by"):
            event["caused_by"] = resolved_objects["caused_by"][0]
        for key in (
            "costs",
            "rewards",
            "output",
            "effects",
            "side_effects",
            "cooldown",
            "pollution_delta",
            "exposure_delta",
            "debt_delta",
        ):
            if key in changes:
                event[key] = changes[key]
        if event_type in {"advantage_reward", "advantage_cost"} and not any(
            key in changes
            for key in (
                "costs",
                "rewards",
                "output",
                "effects",
                "side_effects",
                "cooldown",
                "pollution_delta",
                "exposure_delta",
                "debt_delta",
            )
        ):
            issues.append(
                _advantage_adapter_issue(
                    "ADVANTAGE_RUNTIME_PAYLOAD_REQUIRED",
                    f"{event_type} requires an explicit runtime payload",
                )
            )
            return {"ok": False, "event": None, "issues": issues}

    elif event_type == "advantage_upgrade":
        event["advantage_id"] = subject_id
        if "to_stage" in changes:
            event["to_stage"] = changes["to_stage"]
        if "max_charges" in changes:
            event["max_charges"] = changes["max_charges"]
        if resolved_objects.get("unlock_module"):
            event["unlock_modules"] = list(
                resolved_objects["unlock_module"]
            )

    elif event_type == "advantage_reveal":
        event["knowledge_id"] = subject_id
        event["advantage_id"] = resolved_objects["advantage"][0]
        if resolved_objects.get("module"):
            event["module_id"] = resolved_objects["module"][0]
        if resolved_objects.get("observer"):
            event["observer_entity_id"] = resolved_objects["observer"][0]
        if resolved_objects.get("misread_of"):
            event["misread_of"] = resolved_objects["misread_of"][0]
        for key in ("claim", "reveal_stage", "status", "record_ledger"):
            if key in changes:
                event[key] = changes[key]

    elif event_type == "advantage_contract":
        event.update(
            {
                "action": action,
                "advantage_id": resolved_objects["advantage"][0],
                "contract_id": subject_id,
            }
        )
        if subject_kind == "narrative_contract":
            event["narrative_contract_id"] = subject_id
        if resolved_objects.get("actor"):
            event["actor_entity_id"] = resolved_objects["actor"][0]
        if resolved_objects.get("counterparty"):
            event["counterparty_entity_id"] = resolved_objects[
                "counterparty"
            ][0]
        for key in (
            "contract_status",
            "terms",
            "agency",
            "trust_delta",
            "debt_delta",
            "breach_effect",
            "reading_promise",
            "reward_loop",
            "risk_loop",
            "reveal_ladder",
            "experience_binding",
        ):
            if key in changes:
                event[key] = changes[key]

    elif event_type == "advantage_correction":
        event["action"] = action
        target_id = resolved_objects["target_event"][0]
        if target_id != subject_id:
            issues.append(
                _advantage_adapter_issue(
                    "ADVANTAGE_CORRECTION_TARGET_MISMATCH",
                    "Advantage correction subject and target_event differ",
                    role="target_event",
                    mention=roles["target_event"][0],
                    reference_type="advantage_event",
                    details={
                        "subject_event_id": subject_id,
                        "target_event_id": target_id,
                    },
                )
            )
            return {"ok": False, "event": None, "issues": issues}
        event["target_event_id"] = target_id
        replacement = changes.get("replacement")
        if action != "retract":
            replacement_result = adapt_advantage_extraction_candidate(
                replacement,
                assistant_text,
                context,
                resolver,
                min_confidence=min_confidence,
            )
            issues.extend(replacement_result.get("issues") or [])
            replacement_event = replacement_result.get("event")
            if not isinstance(replacement_event, Mapping):
                return {"ok": False, "event": None, "issues": issues}
            event["replacement"] = dict(replacement_event)
            event["advantage_id"] = str(
                replacement_event.get("advantage_id") or ""
            )
        else:
            advantage_id = str(
                subject_metadata.get("advantage_id")
                or context.get("advantage_id")
                or ""
            ).strip()
            if not advantage_id:
                issues.append(
                    _advantage_adapter_issue(
                        "ADVANTAGE_REFERENCE_UNRESOLVED",
                        "retract correction target did not resolve its "
                        "owning advantage_id",
                        role="target_event",
                        mention=roles["target_event"][0],
                        reference_type="advantage_event",
                        details={"target_event_id": target_id},
                    )
                )
                return {"ok": False, "event": None, "issues": issues}
            event["advantage_id"] = advantage_id

    if not event.get("advantage_id"):
        issues.append(
            _advantage_adapter_issue(
                "ADVANTAGE_REFERENCE_UNRESOLVED",
                "typed Advantage event has no resolved advantage_id",
                role="advantage",
                mention=roles.get("advantage", [""])[0]
                if roles.get("advantage")
                else subject_mention,
                reference_type="advantage",
            )
        )
        return {"ok": False, "event": None, "issues": issues}

    if any(issue.get("severity") == "error" for issue in issues):
        return {"ok": False, "event": None, "issues": issues}
    validated_event = _validate_adapted_advantage_event(
        event,
        context,
        issues,
    )
    if validated_event is None:
        return {"ok": False, "event": None, "issues": issues}
    return {"ok": True, "event": validated_event, "issues": issues}


def adapt_advantage_extraction_candidates(
    candidates: Sequence[Mapping[str, Any]],
    assistant_text: str,
    artifact_context: Mapping[str, Any],
    resolver: Callable[[str, str, str], Any],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Adapt an ordered batch, retaining valid events when peers fail."""

    if (
        not isinstance(candidates, Sequence)
        or isinstance(candidates, (str, bytes, bytearray))
    ):
        raise StateRagError("Advantage candidates must be an array")
    if not isinstance(artifact_context, Mapping):
        raise StateRagError("artifact_context must be an object")
    if not callable(resolver):
        raise StateRagError("Advantage reference resolver must be callable")

    events: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    bindings = artifact_context.get("advantage_experience_bindings")
    if bindings is None:
        bindings = {}
    if not isinstance(bindings, Mapping):
        raise StateRagError("advantage_experience_bindings must be an object")
    requires_contract = _advantage_experience_required(artifact_context)
    failed_creators: dict[tuple[str, str], int] = {}

    def creator_key(raw: Mapping[str, Any]) -> tuple[str, str] | None:
        event_type = str(raw.get("event_type") or "").strip().casefold()
        action = str(raw.get("action") or "").strip().casefold()
        subject = raw.get("subject")
        if not isinstance(subject, Mapping):
            return None
        subject_kind = str(subject.get("kind") or "").strip().casefold()
        mention = str(subject.get("mention") or "").strip().casefold()
        creator_types = {
            ("advantage_spec", "define", "advantage_definition"): (
                "advantage"
            ),
            ("advantage_anchor", "define", "advantage_anchor"): (
                "advantage_anchor"
            ),
            ("advantage_module", "define", "advantage_module"): (
                "advantage_module"
            ),
            ("advantage_reveal", "reveal", "advantage_knowledge"): (
                "advantage_knowledge"
            ),
            ("advantage_contract", "define", "advantage_contract"): (
                "advantage_contract"
            ),
            ("advantage_contract", "narrative", "narrative_contract"): (
                "narrative_contract"
            ),
        }
        reference_type = creator_types.get(
            (event_type, action, subject_kind)
        )
        if reference_type and mention:
            return reference_type, mention
        return None

    def reference_keys(
        raw: Mapping[str, Any],
    ) -> set[tuple[str, str]]:
        result: set[tuple[str, str]] = set()
        subject = raw.get("subject")
        if isinstance(subject, Mapping):
            subject_kind = str(
                subject.get("kind") or ""
            ).strip().casefold()
            mention = str(subject.get("mention") or "").strip().casefold()
            if subject_kind and mention:
                try:
                    result.add(
                        (
                            _advantage_adapter_subject_reference_type(
                                subject_kind
                            ),
                            mention,
                        )
                    )
                except KeyError:
                    pass
        changes = raw.get("changes")
        anchor_type = (
            str(changes.get("anchor_type") or "")
            if isinstance(changes, Mapping)
            else ""
        )
        objects = raw.get("objects")
        if isinstance(objects, list):
            for reference in objects:
                if not isinstance(reference, Mapping):
                    continue
                role = str(reference.get("role") or "").strip().casefold()
                mention = str(
                    reference.get("mention") or ""
                ).strip().casefold()
                if role and mention:
                    result.add(
                        (
                            _advantage_adapter_reference_type_for_role(
                                role,
                                anchor_type=anchor_type,
                            ),
                            mention,
                        )
                    )
        return result

    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            issues.append(
                _advantage_adapter_issue(
                    "ADVANTAGE_EXTRACTION_CANDIDATE_INVALID",
                    f"Advantage candidate {index} must be an object",
                    details={
                        "candidate_index": index,
                        "adapter_stage": "batch_contract",
                    },
                )
            )
            continue
        blocked = sorted(
            (
                key,
                failed_creators[key],
            )
            for key in reference_keys(candidate)
            if key in failed_creators
        )
        if blocked:
            (reference_type, mention), creator_index = blocked[0]
            issues.append(
                _advantage_adapter_issue(
                    "ADVANTAGE_DEPENDENCY_UNRESOLVED",
                    "Advantage candidate depends on an earlier creator "
                    "candidate that failed adaptation",
                    mention=mention,
                    reference_type=reference_type,
                    details={
                        "candidate_index": index,
                        "creator_candidate_index": creator_index,
                        "adapter_stage": "batch_dependency",
                    },
                )
            )
            failed_key = creator_key(candidate)
            if failed_key is not None:
                failed_creators.setdefault(failed_key, index)
            continue
        binding = bindings.get(index)
        if binding is None:
            binding = bindings.get(str(index))
        candidate_context = dict(artifact_context)
        if isinstance(binding, Mapping):
            candidate_context.update(dict(binding))
        elif (
            requires_contract
            and not str(
                candidate_context.get("experience_contract_id") or ""
            ).strip()
        ):
            issues.append(
                _advantage_adapter_issue(
                    "ADVANTAGE_EXPERIENCE_CONTRACT_REQUIRED",
                    "lifecycle-bound Advantage candidate has no local "
                    "experience contract binding",
                    details={
                        "candidate_index": index,
                        "adapter_stage": "experience_binding",
                    },
                )
            )
            continue
        try:
            adapted = adapt_advantage_extraction_candidate(
                candidate,
                assistant_text,
                candidate_context,
                resolver,
                min_confidence=min_confidence,
            )
        except StateRagError as exc:
            issues.append(
                _advantage_adapter_issue(
                    "ADVANTAGE_EXTRACTION_CANDIDATE_INVALID",
                    str(exc),
                    details={
                        "candidate_index": index,
                        "adapter_stage": "candidate_contract",
                    },
                )
            )
            continue
        for raw_issue in adapted.get("issues") or []:
            issue = dict(raw_issue)
            details = dict(issue.get("details") or {})
            details.setdefault("candidate_index", index)
            issue["details"] = details
            issues.append(issue)
        event = adapted.get("event")
        if isinstance(event, Mapping):
            events.append(dict(event))
        else:
            failed_key = creator_key(candidate)
            if failed_key is not None:
                failed_creators.setdefault(failed_key, index)

    return {
        "ok": not any(
            str(issue.get("severity") or "error").casefold()
            in {"error", "critical"}
            for issue in issues
        ),
        "events": events,
        "issues": issues,
        "candidate_count": len(candidates),
        "adapted_count": len(events),
    }


def _chat_extract(
    config: RuntimeConfig,
    assistant_text: str,
    prompt: str,
    current_facts: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    facts_text = "\n".join(_render_fact(fact) for fact in current_facts[:50]) or "(none)"
    if config.version >= 3:
        action_guide = "; ".join(
            f"{event_type}={'|'.join(sorted(actions))}"
            for event_type, actions in _V3_ACTIONS.items()
        )
        legacy_example = {
            "schema_version": DELTA_V4_SCHEMA,
            "deltas": [
                {
                    "event_type": "state",
                    "action": "set",
                    "subject": "测试角色甲",
                    "object": None,
                    "field": "injury",
                    "value": "稳定",
                    "scope": "current",
                    "knowledge_plane": "objective",
                    "confidence": 0.99,
                    "evidence": "测试角色甲伤势稳定。",
                }
            ],
        }
        item_action_guide = "; ".join(
            f"{event_type}={'|'.join(sorted(actions))}"
            for event_type, actions in _ITEM_V4_ACTIONS.items()
        )
        item_roles_guide = {
            event_type: sorted(roles)
            for event_type, roles in _ITEM_V4_OBJECT_ROLES.items()
        }
        item_changes_guide = {
            event_type: sorted(keys)
            for event_type, keys in _ITEM_V4_CHANGE_KEYS.items()
        }
        advantage_action_guide = "; ".join(
            f"{event_type}={'|'.join(sorted(actions))}"
            for event_type, actions in _ADVANTAGE_V4_ACTIONS.items()
        )
        advantage_roles_guide = {
            event_type: sorted(roles)
            for event_type, roles in _ADVANTAGE_V4_OBJECT_ROLES.items()
        }
        advantage_changes_guide = {
            event_type: sorted(keys)
            for event_type, keys in _ADVANTAGE_V4_CHANGE_KEYS.items()
        }
        item_example = {
            "schema_version": DELTA_V4_SCHEMA,
            "deltas": [
                {
                    "event_type": "item_custody",
                    "action": "handover",
                    "subject": {
                        "kind": "item_instance",
                        "mention": "临时通行牌甲",
                    },
                    "objects": [
                        {"role": "from_carrier", "mention": "测试角色乙"},
                        {"role": "to_carrier", "mention": "测试角色甲"},
                    ],
                    "changes": {"quantity": 1},
                    "scope": "current",
                    "story_coordinate": {
                        "calendar_id": "story-main",
                        "ordinal": 17,
                    },
                    "knowledge_plane": "objective",
                    "confidence": 0.99,
                    "evidence": (
                        "在story-main第17刻，测试角色乙把临时通行牌甲交给测试角色甲保管。"
                    ),
                }
            ],
        }
        advantage_example = {
            "schema_version": DELTA_V4_SCHEMA,
            "deltas": [
                {
                    "event_type": "advantage_use",
                    "action": "use",
                    "subject": {
                        "kind": "advantage",
                        "mention": "样例优势核心",
                    },
                    "objects": [
                        {"role": "module", "mention": "状态解析"},
                        {"role": "actor", "mention": "测试角色甲"},
                    ],
                    "changes": {
                        "costs": {"演算点": 1},
                        "effects": ["辨明异常能量"],
                    },
                    "scope": "current",
                    "story_coordinate": {
                        "calendar_id": "story-main",
                        "ordinal": 17,
                    },
                    "knowledge_plane": "objective",
                    "confidence": 0.99,
                    "evidence": (
                        "在story-main第17刻，测试角色甲消耗一缕演算点，以样例优势核心的状态解析模块辨明了异常能量。"
                    ),
                }
            ],
        }
        system = (
            "You extract durable typed story deltas for plot-rag-delta/v4. "
            "Return one JSON object only with exactly schema_version and deltas. "
            "schema_version MUST be plot-rag-delta/v4. Existing legacy event "
            "families other than Item and Advantage retain the frozen "
            "plot-rag-delta/v3 field set and semantics. "
            "Every non-item delta MUST contain "
            "event_type, action, subject, object, field, value, confidence, and "
            "evidence. Optional keys are scope, effective_at, story_coordinate, "
            "knowledge_plane, and ambiguity. Never generate stable IDs. Put unresolved "
            "canonical mentions in subject/object and event-specific details in value. "
            "Never infer unstated facts. evidence must be an exact contiguous quote from "
            "ASSISTANT_TEXT. Omit conditional, uncertain, alternative, or suggested "
            "branches. Mark future commitments planned and past background historical. "
            "Events completed inside the active generated scene are current even when "
            "the prose uses past-tense grammar; historical is only explicit backstory "
            "before the active scene, and planned is only an unrealized future event. "
            "Use objective unless the sentence explicitly expresses belief, rumor, "
            "reader disclosure, or author plan. "
            "Extract every explicit durable location and story-time statement even "
            "when the same ASSISTANT_TEXT also contains state, inventory, or relation "
            "changes. For movement, use arrive/enter when the text only establishes "
            "a current destination and move when it explicitly gives both origin and "
            "destination. Never use action=set for movement. "
            "For frozen non-item v3-shaped deltas only, omit story_coordinate unless "
            "ASSISTANT_TEXT explicitly provides both a stable calendar_id and an "
            "integer ordinal; put ordinary human-readable time labels in effective_at "
            "and the time event value instead. "
            "world_rule is only for durable world mechanics or standing laws; never use "
            "it for a one-time alarm, weather beat, transient scene condition, or mere "
            "observation. "
            "Use exactly one action from this event-family mapping; never echo "
            "the event_type as the action: "
            + action_guide
            + ". "
            "Power mapping: ability uses subject=owner, object=ability and actions "
            "gain/set/use/cooldown/breakthrough/lose/unlock/upgrade/charge/activate/"
            "deactivate/refresh; progression uses subject=actor, object=track and value "
            "{from_rank,to_rank,rank_edge}; resource uses subject=actor, object=resource "
            "and value {amount,target_resource,target_amount,conversion_rule,source}; "
            "status_effect uses subject=actor, object=status and value {stacks,source,"
            "expires_coordinate}; power_binding uses subject=actor, object=source and "
            "value {binding_id,source_type,ability_ids,slot_key,unique}; qualification "
            "uses subject=actor, object=qualification and value {quantity,source}; "
            "power_observation uses subject=observer, object=observed subject and value "
            "{ability,observed_fields}. Do not emit power-system definitions from normal "
            "story prose. "
            "For item_spec/item_instance/item_custody/item_runtime/"
            "item_function_runtime/item_use/item_observation/item_correction, "
            "use only the neutral v4 item shape: "
            "event_type, action, subject={kind,mention}, objects=[{role,mention}], "
            "changes, scope, story_coordinate, knowledge_plane, confidence, evidence, "
            "with only effective_at and ambiguity optional. Item subject/object mentions "
            "and evidence must occur verbatim in the same contiguous evidence quote. "
            "Every item v4 candidate MUST include story_coordinate. Copy its calendar_id "
            "and integer ordinal only from ASSISTANT_TEXT or trusted CURRENT_FACTS. If "
            "neither source contains both values, omit that item candidate; never invent "
            "a coordinate. "
            "Use exactly one item action from this mapping: "
            + item_action_guide
            + ". Allowed item object roles are "
            + _json_dumps(item_roles_guide)
            + "; allowed item change keys are "
            + _json_dumps(item_changes_guide)
            + ". The model may report only the explicit action, participating mentions, "
            "explicit non-negative quantity/charge/durability/energy/cooldown "
            "magnitudes (zero is valid when the text explicitly states a bootstrap "
            "or set_charges value), "
            "explicit observable effects, a comparable {calendar_id,ordinal} story "
            "coordinate, and the verbatim evidence. Never emit before/after/current/"
            "remaining/resulting/computed/derived/new/updated state or counters. "
            "Absolute values are otherwise forbidden except explicitly stated "
            "definition fields, instance metadata (batch, attributes, instance_name, "
            "serial_or_mark, unique, provenance), custody_status, bootstrap runtime "
            "state, item_function_runtime changes.remaining_charges for "
            "bootstrap/set_charges, and changes.cooldown_until for bootstrap/set_cooldown. "
            "For item_runtime bootstrap, equipped_by plus slot_key must be emitted "
            "together when the item is equipped; bound_actor is a separate explicit "
            "binding. "
            "Never emit database IDs. The local adapter resolves stable identity and the local "
            "reducer alone reads before state and computes after state, conservation, "
            "ownership, custody, charges, durability, energy, quantity, and cooldown. "
            "Never emit legacy_v3_delta or item_v4_candidate wrapper keys: each deltas "
            "entry is the raw event object. An empty deltas array is valid. "
            "For advantage_spec/advantage_anchor/advantage_module/"
            "advantage_bind/advantage_activate/advantage_trigger/"
            "advantage_use/advantage_reward/advantage_cost/"
            "advantage_upgrade/advantage_reveal/advantage_contract/"
            "advantage_correction, use the same neutral v4 candidate shape: "
            "event_type, action, subject={kind,mention}, "
            "objects=[{role,mention}], changes, scope, story_coordinate, "
            "knowledge_plane, confidence, evidence, with only effective_at "
            "and ambiguity optional. Every Advantage mention must occur "
            "verbatim in the same contiguous evidence quote. Every Advantage "
            "candidate MUST include story_coordinate copied only from "
            "ASSISTANT_TEXT or trusted CURRENT_FACTS; omit the candidate when "
            "calendar_id plus integer ordinal are unavailable. Use exactly one "
            "Advantage action from this mapping: "
            + advantage_action_guide
            + ". Allowed Advantage object roles are "
            + _json_dumps(advantage_roles_guide)
            + "; allowed Advantage change keys are "
            + _json_dumps(advantage_changes_guide)
            + ". Never output plot-rag-advantage/v1 from the remote model. "
            "Never emit advantage_id, anchor_id, module_id, knowledge_id, "
            "contract_id, event IDs, experience_contract_id, "
            "experience_contract_hash, event_seed_id, record_only, or any "
            "other database/control identifier. Never emit before/after/"
            "current/remaining/resulting/computed/derived/new/updated state. "
            "Normal Stop extraction may report explicit runtime deltas, costs, "
            "rewards, effects, cooldown magnitude, stage, and max_charges only "
            "where the closed change mapping permits them; it must not report "
            "activate-time absolute charges/resources/pollution/exposure/debt/"
            "cooldown snapshots. The local adapter alone resolves or "
            "predeclares stable identity, binds the locked Event Experience "
            "contract, and converts the neutral candidate to "
            "plot-rag-advantage/v1; the local reducer alone computes runtime "
            "state. "
            "BEGIN_VALID_NON_ITEM_ENVELOPE_EXAMPLE "
            + _json_dumps(legacy_example)
            + " END_VALID_NON_ITEM_ENVELOPE_EXAMPLE "
            "BEGIN_VALID_ITEM_ENVELOPE_EXAMPLE "
            + _json_dumps(item_example)
            + " END_VALID_ITEM_ENVELOPE_EXAMPLE "
            "BEGIN_VALID_ADVANTAGE_ENVELOPE_EXAMPLE "
            + _json_dumps(advantage_example)
            + " END_VALID_ADVANTAGE_ENVELOPE_EXAMPLE"
        )
    else:
        schema = {
            "deltas": [
                {
                    "category": "character_state|relationship|location|inventory|story_time|world_state",
                    "subject": "canonical entity or scope",
                    "field": "stable field name",
                    "operation": "set|delete",
                    "scope": "current|planned|historical; omit only when clearly current",
                    "effective_at": "optional in-story time label",
                    "value": "any JSON value; null only for delete",
                    "confidence": 0.0,
                    "evidence": "exact contiguous quote from ASSISTANT_TEXT",
                }
            ]
        }
        system = (
            "You extract durable story-state deltas. Return one JSON object only. "
            "Every delta MUST contain all seven required keys: category, subject, field, operation, "
            "value, confidence, evidence. The only optional keys are scope and effective_at. Never "
            "omit subject or value even when they seem obvious. "
            "Never infer unstated facts. Every evidence string must be an exact contiguous quote "
            "from ASSISTANT_TEXT. Use only the allowed categories, operations, and scopes. Omit "
            "conditional, uncertain, alternative, or suggested branches. Mark future commitments "
            "as planned and past background as historical; only clearly established final/current "
            "state may use current. For relationship set value must be an object containing target; "
            "relationship fields are normalized to to:<target>. For inventory, identify the item in "
            "value.item/value.name or a specific field; never use a generic inventory field. Use "
            "field=current for location and story_time; location value is the explicit place string, "
            "and story_time uses subject=故事 with the explicit time string as value. An empty deltas "
            "array is valid. Schema: "
            + _json_dumps(schema)
        )
    user = (
        "USER_PROMPT:\n<<<\n"
        + prompt
        + "\n>>>\nCURRENT_FACTS:\n<<<\n"
        + facts_text
        + "\n>>>\nASSISTANT_TEXT:\n<<<\n"
        + assistant_text
        + "\n>>>"
    )
    payload = {
        "model": config.extract.model,
        "temperature": 0,
        "max_tokens": config.extract.max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    response, status = _remote_json(config.extract, payload)
    attempts = 1
    repair_reason = ""
    try:
        extracted = _decode_chat_completion(
            response,
            require_explicit_stop=config.version >= 3,
        )
    except StateRagError as decode_error:
        if config.version < 3:
            raise
        repair_payload = dict(payload)
        repair_payload["messages"] = [
            {
                "role": "system",
                "content": (
                    system
                    + " The previous attempt was invalid or truncated. "
                    "Return a complete corrected envelope now. Decode error: "
                    + str(decode_error)
                ),
            },
            {"role": "user", "content": user},
        ]
        response, status = _remote_json(config.extract, repair_payload)
        attempts = 2
        repair_reason = "decode_or_truncation"
        extracted = _decode_chat_completion(
            response,
            require_explicit_stop=config.version >= 3,
        )
        deltas, skipped = _validate_deltas(
            extracted,
            assistant_text,
            config,
            prompt,
        )
    else:
        try:
            deltas, skipped = _validate_deltas(
                extracted,
                assistant_text,
                config,
                prompt,
            )
        except StateRagError as validation_error:
            validation_error_text = str(validation_error)
            if (
                config.version < 3
                or _has_protected_story_coordinate(extracted)
                or _has_advantage_delta(extracted)
            ):
                raise
            targeted_coordinate_repair = (
                "story_coordinate" in validation_error_text
                and not _has_power_or_item_delta(extracted)
            )
            if targeted_coordinate_repair:
                repair_system = (
                    system
                    + " Repair only story_coordinate/effective_at and, "
                    "for time events, value. Keep delta count, order, "
                    "event types, entities, evidence, confidence, scope, "
                    "and all unrelated values unchanged. Validation "
                    "error: "
                    + validation_error_text
                )
                repair_user = user
                repair_reason = "story_coordinate"
            else:
                repair_system, repair_user = _validation_repair_messages(
                    system=system,
                    user=user,
                    invalid_envelope=extracted,
                    validation_error=validation_error,
                )
                repair_reason = "validation"
            repair_payload = dict(payload)
            repair_payload["messages"] = [
                {"role": "system", "content": repair_system},
                {"role": "user", "content": repair_user},
            ]
            response, status = _remote_json(config.extract, repair_payload)
            attempts = 2
            repaired = _decode_chat_completion(
                response,
                require_explicit_stop=True,
            )
            if targeted_coordinate_repair:
                _validate_targeted_extraction_repair(extracted, repaired)
            deltas, skipped = _validate_deltas(
                repaired,
                assistant_text,
                config,
                prompt,
            )

    if config.version >= 3:
        units = _coverage_units(assistant_text, deltas)
        missing_units = _missing_coverage_units(units, deltas)
        if missing_units and attempts == 1:
            repair_system, repair_user = _coverage_repair_messages(
                assistant_text=assistant_text,
                prompt=prompt,
                missing_units=missing_units,
                schema={
                    **legacy_example,
                    "schema_version": DELTA_V3_SCHEMA,
                },
            )
            repair_payload = {
                "model": config.extract.model,
                "temperature": 0,
                "max_tokens": config.extract.max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": repair_system},
                    {"role": "user", "content": repair_user},
                ],
            }
            response, status = _remote_json(config.extract, repair_payload)
            attempts = 2
            repair_reason = "coverage"
            supplemental = _decode_chat_completion(
                response,
                require_explicit_stop=True,
            )
            supplement_deltas, supplement_skipped = _validate_deltas(
                supplemental,
                assistant_text,
                config,
                prompt,
            )
            allowed_types = {
                str(event_type)
                for unit in missing_units
                for event_type in unit.get("event_types") or []
            }
            allowed_quotes = {
                str(unit.get("quote") or "") for unit in missing_units
            }
            for delta in supplement_deltas:
                event_type = str(delta.get("event_type") or "")
                if event_type not in allowed_types:
                    raise StateRagError(
                        "EXTRACTION_COVERAGE_REPAIR_ADDED_UNRELATED_EVENT"
                    )
                evidence = str(delta.get("evidence") or "")
                if not any(evidence in quote for quote in allowed_quotes):
                    raise StateRagError(
                        "EXTRACTION_COVERAGE_REPAIR_EVIDENCE_OUTSIDE_UNIT"
                    )
                if event_type == "movement":
                    subject = str(delta.get("subject") or "").strip()
                    destination = str(delta.get("object") or "").strip()
                    if (
                        not subject
                        or not destination
                        or subject not in evidence
                        or destination not in evidence
                    ):
                        raise StateRagError(
                            "EXTRACTION_COVERAGE_REPAIR_ANCHOR_MISMATCH"
                        )
                elif event_type == "time":
                    coordinate = delta.get("story_coordinate")
                    labels = {
                        str(delta.get("value") or "").strip(),
                        str(delta.get("effective_at") or "").strip(),
                    }
                    if isinstance(coordinate, dict):
                        labels.add(str(coordinate.get("label") or "").strip())
                    labels.discard("")
                    if not labels or not any(
                        label in evidence for label in labels
                    ):
                        raise StateRagError(
                            "EXTRACTION_COVERAGE_REPAIR_ANCHOR_MISMATCH"
                        )
            fingerprints = {_json_dumps(delta) for delta in deltas}
            for delta in supplement_deltas:
                fingerprint = _json_dumps(delta)
                if fingerprint not in fingerprints:
                    deltas.append(delta)
                    fingerprints.add(fingerprint)
            skipped.extend(supplement_skipped)
            remaining = _missing_coverage_units(missing_units, deltas)
            if remaining:
                raise StateRagError(
                    "EXTRACTION_COVERAGE_INCOMPLETE: "
                    + ",".join(
                        str(unit.get("unit_id") or "")
                        + "="
                        + "|".join(unit.get("event_types") or [])
                        for unit in remaining
                    )
                )
        elif missing_units:
            raise StateRagError(
                "EXTRACTION_COVERAGE_INCOMPLETE: "
                + ",".join(
                    str(unit.get("unit_id") or "")
                    + "="
                    + "|".join(unit.get("event_types") or [])
                    for unit in missing_units
                )
            )
    status = dict(status)
    status["attempts"] = attempts
    if attempts == 2:
        status["repair_applied"] = True
        status["repair_reason"] = repair_reason
    status["generation_identity"] = _extraction_generation_params(config)
    status["tool_shadow"] = _run_extraction_tool_shadow(
        config=config,
        authoritative_payload=payload,
        assistant_text=assistant_text,
        prompt=prompt,
        authoritative_deltas=deltas,
        authoritative_skipped=skipped,
    )
    return deltas, skipped, status


def _run_extraction_tool_shadow(
    *,
    config: RuntimeConfig,
    authoritative_payload: Mapping[str, Any],
    assistant_text: str,
    prompt: str,
    authoritative_deltas: Sequence[Mapping[str, Any]],
    authoritative_skipped: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Run a validator-only tool-schema comparison with no proposal output."""

    identity = _extraction_protocol_identity(config)["tool_shadow"]
    diagnostics: dict[str, Any] = {
        **identity,
        "status": (
            "not_called" if identity["enabled"] else "disabled"
        ),
        "validator_status": "not_called",
        "equivalent": None,
    }
    if not identity["enabled"]:
        return diagnostics

    raw_messages = authoritative_payload.get("messages")
    if not isinstance(raw_messages, list):
        diagnostics.update(
            {
                "status": "failed",
                "validator_status": "not_called",
                "reason": "authoritative messages are unavailable",
            }
        )
        return diagnostics
    messages = [
        dict(message) if isinstance(message, Mapping) else message
        for message in raw_messages
    ]
    if messages and isinstance(messages[0], dict):
        messages[0] = {
            **messages[0],
            "content": (
                str(messages[0].get("content") or "")
                + " For this shadow trial, call the required function exactly "
                "once and put the same extraction envelope in its arguments. "
                "Do not return the envelope as message content."
            ),
        }
    tool_schema = _extraction_tool_schema(config.version)
    shadow_payload = {
        "model": config.extract.model,
        "temperature": 0,
        "max_tokens": config.extract.max_tokens,
        "messages": messages,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": identity["tool_name"],
                    "description": (
                        "Submit evidence-backed durable story deltas for "
                        "local validation."
                    ),
                    "parameters": tool_schema,
                },
            }
        ],
        "tool_choice": {
            "type": "function",
            "function": {"name": identity["tool_name"]},
        },
        "parallel_tool_calls": False,
    }
    try:
        response, remote_status = _remote_json(
            config.extract,
            shadow_payload,
        )
        extracted = _decode_chat_tool_call(
            response,
            expected_tool_name=str(identity["tool_name"]),
        )
        shadow_deltas, shadow_skipped = _validate_deltas(
            extracted,
            assistant_text,
            config,
            prompt,
        )
    except StateRagError as exc:
        diagnostics.update(
            {
                "status": "failed",
                "validator_status": "failed",
                "reason": str(exc),
            }
        )
        return diagnostics

    authoritative_hash = _sha256_json(
        {
            "deltas": list(authoritative_deltas),
            "skipped": list(authoritative_skipped),
        }
    )
    shadow_hash = _sha256_json(
        {
            "deltas": shadow_deltas,
            "skipped": shadow_skipped,
        }
    )
    equivalent = authoritative_hash == shadow_hash
    diagnostics.update(
        {
            "status": "equivalent" if equivalent else "mismatch",
            "validator_status": "passed",
            "equivalent": equivalent,
            "authoritative_result_hash": authoritative_hash,
            "shadow_result_hash": shadow_hash,
            "remote": {
                key: value
                for key, value in remote_status.items()
                if key
                in {
                    "status",
                    "http_status",
                    "latency_ms",
                    "service",
                    "model",
                }
            },
        }
    )
    return diagnostics


def _validate_deltas(
    extracted: Any,
    assistant_text: str,
    config: RuntimeConfig,
    prompt: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate typed Stop output while preserving pre-v3 compatibility.

    Config v3 is the strict lifecycle boundary.  Explicit v3 envelopes retain
    their frozen legacy semantics; v4 envelopes may mix those exact legacy
    deltas with neutral item candidates.  A typed config never silently
    re-enters the pre-v3 generic-state adapter.
    """

    if (
        isinstance(extracted, dict)
        and extracted.get("schema_version") == DELTA_V4_SCHEMA
    ):
        if config.version < 3:
            raise StateRagError(
                "plot-rag-delta/v4 requires config version >= 3"
            )
        return validate_delta_v4_envelope(
            extracted,
            assistant_text,
            config,
            prompt,
        )
    if (
        isinstance(extracted, dict)
        and extracted.get("schema_version") == DELTA_V3_SCHEMA
    ):
        return _validate_v3_deltas(
            extracted,
            assistant_text,
            config,
            prompt,
        )
    if config.version >= 3:
        return validate_delta_v4_envelope(
            extracted,
            assistant_text,
            config,
            prompt,
        )
    return _validate_legacy_deltas(
        extracted,
        assistant_text,
        config,
        prompt,
    )


def _validate_v3_deltas(
    extracted: Any,
    assistant_text: str,
    config: RuntimeConfig,
    prompt: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if (
        not isinstance(extracted, dict)
        or set(extracted) != {"schema_version", "deltas"}
        or extracted.get("schema_version") != DELTA_V3_SCHEMA
    ):
        raise StateRagError(
            "typed extraction JSON must contain schema_version=plot-rag-delta/v3 and deltas"
        )
    raw_deltas = extracted["deltas"]
    if not isinstance(raw_deltas, list) or len(raw_deltas) > 500:
        raise StateRagError("extraction deltas must be an array with at most 500 items")
    required = {
        "event_type",
        "action",
        "subject",
        "object",
        "field",
        "value",
        "confidence",
        "evidence",
    }
    optional = {
        "scope",
        "effective_at",
        "story_coordinate",
        "knowledge_plane",
        "ambiguity",
    }
    coordinate_keys = {
        "calendar_id",
        "ordinal",
        "label",
        "precision",
        "source_event_id",
    }
    result: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    alternative_request = bool(_ALTERNATIVE_REQUEST_RE.search(prompt)) and not bool(
        re.search(r"(?:不要|不需要|无需|拒绝).{0,8}(?:方案|备选|选项)", prompt)
    )
    for index, raw in enumerate(raw_deltas):
        keys = set(raw) if isinstance(raw, dict) else set()
        if (
            not isinstance(raw, dict)
            or not required.issubset(keys)
            or not keys.issubset(required | optional)
        ):
            raise StateRagError(
                f"deltas[{index}] must contain the typed v3 required keys and only supported optional keys"
            )
        raw_event_type = raw["event_type"]
        if not isinstance(raw_event_type, str) or not raw_event_type.strip():
            raise StateRagError(f"deltas[{index}].event_type is invalid")
        event_type = _V3_EVENT_ALIASES.get(
            raw_event_type.strip().casefold(),
            raw_event_type.strip().casefold(),
        )
        category = _V3_EVENT_CATEGORIES.get(event_type)
        if category is None:
            raise StateRagError(
                f"deltas[{index}].event_type is unsupported: {event_type}"
            )
        if category not in config.categories:
            raise StateRagError(
                f"deltas[{index}].event_type category is not enabled: {category}"
            )
        action = raw["action"]
        if not isinstance(action, str):
            raise StateRagError(f"deltas[{index}].action is invalid")
        action = action.strip().casefold()
        echoed_action = _V3_EVENT_ALIASES.get(action, action)
        if (
            echoed_action == event_type
            and event_type in _V3_SINGLE_ACTION_ECHO_REPAIRS
        ):
            action = _V3_SINGLE_ACTION_ECHO_REPAIRS[event_type]
        movement_set_needs_repair = event_type == "movement" and action == "set"
        if not movement_set_needs_repair and action not in _V3_ACTIONS[event_type]:
            raise StateRagError(
                f"deltas[{index}].action is unsupported for {event_type}: {action}"
            )
        subject = raw["subject"]
        object_value = raw["object"]
        field = raw["field"]
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or len(subject.strip()) > MAX_SUBJECT_CHARS
        ):
            raise StateRagError(f"deltas[{index}].subject is invalid")
        if object_value is not None and (
            not isinstance(object_value, str)
            or not object_value.strip()
            or len(object_value.strip()) > MAX_SUBJECT_CHARS
        ):
            raise StateRagError(f"deltas[{index}].object is invalid")
        if field is not None and (
            not isinstance(field, str)
            or len(field.strip()) > MAX_FIELD_CHARS
        ):
            raise StateRagError(f"deltas[{index}].field is invalid")
        confidence = raw["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise StateRagError(f"deltas[{index}].confidence must be numeric")
        confidence = float(confidence)
        if not math.isfinite(confidence) or not config.min_confidence <= confidence <= 1.0:
            raise StateRagError(
                f"deltas[{index}].confidence is below {config.min_confidence} or above 1"
            )
        evidence = raw["evidence"]
        if not isinstance(evidence, str) or not evidence or len(evidence) > MAX_EVIDENCE_CHARS:
            raise StateRagError(f"deltas[{index}].evidence is invalid")
        if evidence not in assistant_text:
            raise StateRagError(
                f"deltas[{index}].evidence is not an exact quote from assistant_text"
            )
        evidence_offset = assistant_text.find(evidence)
        evidence_context = assistant_text[
            max(0, evidence_offset - 120) : min(
                len(assistant_text), evidence_offset + len(evidence) + 40
            )
        ]
        if alternative_request or _ALTERNATIVE_CONTEXT_RE.search(evidence_context):
            skipped.append(
                {
                    "index": index,
                    "reason": "alternative_branch",
                    "evidence": evidence,
                }
            )
            continue
        if _UNCERTAIN_RE.search(evidence):
            skipped.append(
                {
                    "index": index,
                    "reason": "uncertain_or_conditional_branch",
                    "evidence": evidence,
                }
            )
            continue
        scope = raw.get("scope", "current")
        if not isinstance(scope, str) or scope not in ALLOWED_SCOPES:
            raise StateRagError(f"deltas[{index}].scope is invalid")
        if scope == "current" and _FUTURE_RE.search(evidence):
            scope = "planned"
        elif scope == "current" and _HISTORICAL_RE.search(evidence):
            scope = "historical"
        knowledge_plane = raw.get("knowledge_plane", "objective")
        if (
            not isinstance(knowledge_plane, str)
            or knowledge_plane not in POWER_KNOWLEDGE_PLANES
        ):
            raise StateRagError(f"deltas[{index}].knowledge_plane is invalid")
        if knowledge_plane == "author_plan" and scope == "current":
            scope = "planned"
        if movement_set_needs_repair:
            normalized_action = _normalize_movement_set_action(
                subject=subject.strip(),
                destination=(
                    object_value.strip()
                    if isinstance(object_value, str)
                    else ""
                ),
                value=raw.get("value"),
                evidence=evidence,
                scope=scope,
                knowledge_plane=knowledge_plane,
            )
            if normalized_action is None:
                raise StateRagError(
                    f"deltas[{index}].action is unsupported for movement: set"
                )
            action = normalized_action
        elif event_type == "movement" and action == "move":
            origin = (
                str(raw["value"].get("from_location") or "").strip()
                if isinstance(raw["value"], dict)
                else ""
            )
            destination = (
                object_value.strip()
                if isinstance(object_value, str)
                else ""
            )
            if not _has_explicit_movement_route(
                subject=subject.strip(),
                origin=origin,
                destination=destination,
                evidence=evidence,
            ):
                raise StateRagError(
                    f"deltas[{index}].movement move requires an explicit "
                    "same-evidence origin and destination"
                )
        effective_at = raw.get("effective_at")
        if effective_at is not None:
            if isinstance(effective_at, bool) or not isinstance(
                effective_at, (str, int, float)
            ):
                raise StateRagError(
                    f"deltas[{index}].effective_at must be a string or number"
                )
            if isinstance(effective_at, float) and not math.isfinite(effective_at):
                raise StateRagError(
                    f"deltas[{index}].effective_at must be finite"
                )
            if isinstance(effective_at, str) and not effective_at.strip():
                raise StateRagError(
                    f"deltas[{index}].effective_at must not be empty"
                )
        value = raw["value"]
        coordinate = raw.get("story_coordinate")
        if coordinate is not None:
            if not isinstance(coordinate, dict) or not set(coordinate).issubset(
                coordinate_keys
            ):
                raise StateRagError(
                    f"deltas[{index}].story_coordinate is invalid"
                )
            ordinal = coordinate.get("ordinal")
            if ordinal is not None and type(ordinal) is not int:
                raise StateRagError(
                    f"deltas[{index}].story_coordinate.ordinal is invalid"
                )
            structural_keys = set(coordinate) - {"label"}
            if not structural_keys:
                label = str(coordinate.get("label") or "").strip()
                if event_type in _POWER_EVENT_TYPES and label:
                    raise StateRagError(
                        f"deltas[{index}].story_coordinate requires "
                        "calendar_id and integer ordinal for power events"
                    )
                if label:
                    if label not in evidence:
                        raise StateRagError(
                            f"deltas[{index}].story_coordinate.label is not "
                            "an exact quote from evidence"
                        )
                    if event_type == "time":
                        if value in (None, "", {}, []):
                            value = label
                        elif isinstance(value, str):
                            if value.strip() != label:
                                raise StateRagError(
                                    f"deltas[{index}].time value conflicts "
                                    "with story_coordinate.label"
                                )
                            value = label
                    if effective_at is None:
                        effective_at = label
                coordinate = None
            else:
                calendar_id = coordinate.get("calendar_id")
                has_comparable_coordinate = bool(
                    isinstance(calendar_id, str)
                    and calendar_id.strip()
                    and type(ordinal) is int
                )
                if not has_comparable_coordinate:
                    suffix = (
                        " for power events"
                        if event_type in _POWER_EVENT_TYPES
                        else ""
                    )
                    raise StateRagError(
                        f"deltas[{index}].story_coordinate requires "
                        f"calendar_id and integer ordinal{suffix}"
                    )
        try:
            value_json = _json_dumps(value)
            coordinate_json = _json_dumps(coordinate)
            ambiguity_json = _json_dumps(raw.get("ambiguity"))
        except (TypeError, ValueError) as exc:
            raise StateRagError(
                f"deltas[{index}] contains a non-strict JSON value"
            ) from exc
        if (
            len(value_json) > MAX_VALUE_JSON_CHARS
            or len(coordinate_json) > MAX_VALUE_JSON_CHARS
            or len(ambiguity_json) > MAX_VALUE_JSON_CHARS
        ):
            raise StateRagError(f"deltas[{index}] payload is too large")
        delta = {
            "schema_version": DELTA_V3_SCHEMA,
            "event_type": event_type,
            "category": category,
            "action": action,
            "subject": subject.strip(),
            "object": (
                object_value.strip()
                if isinstance(object_value, str)
                else None
            ),
            "field": field.strip() if isinstance(field, str) else None,
            "value": value,
            "scope": scope,
            "effective_at": effective_at,
            "story_coordinate": coordinate,
            "knowledge_plane": knowledge_plane,
            "ambiguity": raw.get("ambiguity"),
            "confidence": confidence,
            "evidence": evidence,
        }
        fingerprint = _json_dumps(delta)
        if fingerprint not in seen:
            result.append(delta)
            seen.add(fingerprint)
    return result, skipped


def _validate_legacy_deltas(
    extracted: Any,
    assistant_text: str,
    config: RuntimeConfig,
    prompt: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(extracted, dict) or set(extracted) != {"deltas"}:
        raise StateRagError("extraction JSON must contain exactly one top-level key: deltas")
    raw_deltas = extracted["deltas"]
    if not isinstance(raw_deltas, list) or len(raw_deltas) > 500:
        raise StateRagError("extraction deltas must be an array with at most 500 items")
    required = {"category", "subject", "field", "operation", "value", "confidence", "evidence"}
    optional = {"scope", "effective_at"}
    result: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    alternative_request = bool(_ALTERNATIVE_REQUEST_RE.search(prompt)) and not bool(
        re.search(r"(?:不要|不需要|无需|拒绝).{0,8}(?:方案|备选|选项)", prompt)
    )
    for index, raw in enumerate(raw_deltas):
        keys = set(raw) if isinstance(raw, dict) else set()
        if not isinstance(raw, dict) or not required.issubset(keys) or not keys.issubset(required | optional):
            raise StateRagError(
                f"deltas[{index}] must contain required keys "
                + ", ".join(sorted(required))
                + " and only optional keys scope/effective_at"
            )
        category = raw["category"]
        if not isinstance(category, str) or category not in config.categories:
            raise StateRagError(f"deltas[{index}].category is not enabled")
        subject = raw["subject"]
        field = raw["field"]
        operation = raw["operation"]
        evidence = raw["evidence"]
        if not isinstance(subject, str) or not subject.strip() or len(subject.strip()) > MAX_SUBJECT_CHARS:
            raise StateRagError(f"deltas[{index}].subject is invalid")
        if not isinstance(field, str) or not field.strip() or len(field.strip()) > MAX_FIELD_CHARS:
            raise StateRagError(f"deltas[{index}].field is invalid")
        if operation not in ALLOWED_OPERATIONS:
            raise StateRagError(f"deltas[{index}].operation must be set or delete")
        confidence = raw["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise StateRagError(f"deltas[{index}].confidence must be numeric")
        confidence = float(confidence)
        if not math.isfinite(confidence) or not config.min_confidence <= confidence <= 1.0:
            raise StateRagError(
                f"deltas[{index}].confidence is below {config.min_confidence} or above 1"
            )
        if not isinstance(evidence, str) or not evidence or len(evidence) > MAX_EVIDENCE_CHARS:
            raise StateRagError(f"deltas[{index}].evidence is invalid")
        if evidence not in assistant_text:
            raise StateRagError(f"deltas[{index}].evidence is not an exact quote from assistant_text")
        evidence_offset = assistant_text.find(evidence)
        evidence_context = assistant_text[
            max(0, evidence_offset - 120) : min(
                len(assistant_text), evidence_offset + len(evidence) + 40
            )
        ]
        if alternative_request or _ALTERNATIVE_CONTEXT_RE.search(evidence_context):
            skipped.append(
                {
                    "index": index,
                    "reason": "alternative_branch",
                    "evidence": evidence,
                }
            )
            continue
        if _UNCERTAIN_RE.search(evidence):
            skipped.append(
                {
                    "index": index,
                    "reason": "uncertain_or_conditional_branch",
                    "evidence": evidence,
                }
            )
            continue
        scope = raw.get("scope", "current")
        if not isinstance(scope, str) or scope not in ALLOWED_SCOPES:
            raise StateRagError(f"deltas[{index}].scope is invalid")
        if scope == "current" and _FUTURE_RE.search(evidence):
            scope = "planned"
        elif scope == "current" and _HISTORICAL_RE.search(evidence):
            scope = "historical"
        effective_at = raw.get("effective_at")
        if effective_at is not None:
            if isinstance(effective_at, bool) or not isinstance(effective_at, (str, int, float)):
                raise StateRagError(f"deltas[{index}].effective_at must be a string or number")
            if isinstance(effective_at, float) and not math.isfinite(effective_at):
                raise StateRagError(f"deltas[{index}].effective_at must be finite")
            if isinstance(effective_at, str) and not effective_at.strip():
                raise StateRagError(f"deltas[{index}].effective_at must not be empty")
        value = raw["value"]
        if operation == "delete" and value is not None:
            raise StateRagError(f"deltas[{index}].value must be null for delete")
        if operation == "set" and value is None:
            raise StateRagError(f"deltas[{index}].value must not be null for set")
        normalized_field = field.strip()
        if category == "relationship":
            if operation == "set":
                if not isinstance(value, dict):
                    raise StateRagError(
                        f"deltas[{index}].value must be an object with target for relationship"
                    )
                target = value.get("target")
                if not isinstance(target, str) or not target.strip():
                    raise StateRagError(
                        f"deltas[{index}].value.target is required for relationship"
                    )
                if len(target.strip()) > MAX_SUBJECT_CHARS:
                    raise StateRagError(f"deltas[{index}].value.target is too long")
                normalized_field = "to:" + target.strip()
            else:
                if not normalized_field.startswith("to:") or not normalized_field[3:].strip():
                    raise StateRagError(
                        f"deltas[{index}].field must be to:<target> for relationship delete"
                    )
                normalized_field = "to:" + normalized_field[3:].strip()
        elif category == "inventory":
            generic = {"inventory", "inventories", "item", "items", "道具", "物品", "持有物"}
            item_name = ""
            if isinstance(value, dict):
                candidate = value.get("item", value.get("name"))
                if isinstance(candidate, str):
                    item_name = candidate.strip()
            if item_name:
                normalized_field = "item:" + item_name
            else:
                specific = normalized_field[5:].strip() if normalized_field.startswith("item:") else normalized_field
                if not specific or specific.casefold() in generic:
                    raise StateRagError(
                        f"deltas[{index}] inventory field must identify one specific item"
                    )
                normalized_field = "item:" + specific
        elif category in {"location", "story_time"}:
            normalized_field = "current"
        if len(normalized_field) > MAX_FIELD_CHARS:
            raise StateRagError(f"deltas[{index}].field is too long after normalization")
        try:
            value_json = _json_dumps(value)
        except (TypeError, ValueError) as exc:
            raise StateRagError(f"deltas[{index}].value is not strict JSON") from exc
        if len(value_json) > MAX_VALUE_JSON_CHARS:
            raise StateRagError(f"deltas[{index}].value is too large")
        delta = {
            "category": category,
            "subject": subject.strip(),
            "field": normalized_field,
            "operation": operation,
            "scope": scope,
            "effective_at": effective_at,
            "value": value,
            "confidence": confidence,
            "evidence": evidence,
        }
        fingerprint = _json_dumps(delta)
        if fingerprint not in seen:
            result.append(delta)
            seen.add(fingerprint)
    return result, skipped


def _fact_key(category: str, subject: str, field: str) -> str:
    return _hash(category, subject.casefold().strip(), field.casefold().strip(), length=40)


def _public_fact(row: sqlite3.Row | dict[str, Any], score: float | None = None) -> dict[str, Any]:
    value = json.loads(str(row["value_json"]))
    result = {
        "fact_key": str(row["fact_key"]),
        "category": str(row["category"]),
        "subject": str(row["subject"]),
        "field": str(row["field"]),
        "scope": str(row["scope"]) if "scope" in row.keys() else "current",
        "effective_at": row["effective_at"] if "effective_at" in row.keys() else None,
        "value": value,
        "confidence": float(row["confidence"]),
        "evidence": str(row["evidence"]),
        "event_id": str(row["event_id"]),
        "updated_at": str(row["updated_at"]),
    }
    if score is not None:
        result["score"] = round(float(score), 6)
    return result


def _public_event(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(row["event_id"]),
        "request_id": str(row["request_id"]),
        "receipt_id": str(row["receipt_id"]),
        "session_id": str(row["session_id"]),
        "category": str(row["category"]),
        "subject": str(row["subject"]),
        "field": str(row["field"]),
        "operation": str(row["operation"]),
        "scope": str(row["scope"]),
        "effective_at": row["effective_at"],
        "value": None if row["value_json"] is None else json.loads(str(row["value_json"])),
        "confidence": float(row["confidence"]),
        "evidence": str(row["evidence"]),
        "source_hash": str(row["source_hash"]),
        "created_at": str(row["created_at"]),
    }


def _render_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _render_fact(fact: dict[str, Any]) -> str:
    scope = fact.get("scope", "current")
    effective = f" @ {fact['effective_at']}" if fact.get("effective_at") is not None else ""
    return (
        f"[{fact['category']} scope={scope}{effective}] "
        f"{fact['subject']}.{fact['field']} = {_render_value(fact['value'])}"
    )


def _tokens(text: str) -> set[str]:
    lowered = str(text or "").casefold()
    tokens = set(_WORD_RE.findall(lowered))
    for block in _CJK_RE.findall(lowered):
        tokens.update(block)
        tokens.update(block[index : index + 2] for index in range(max(0, len(block) - 1)))
        tokens.update(block[index : index + 3] for index in range(max(0, len(block) - 2)))
    return {token for token in tokens if token}


def _lexical_score(query: str, document: str) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    document_tokens = _tokens(document)
    overlap = sum(1 for token in query_tokens if token in document_tokens)
    coverage = overlap / max(1, len(query_tokens))
    phrase = 1.0 if query.casefold().strip() in document.casefold() else 0.0
    return min(1.0, 0.82 * coverage + 0.18 * phrase)


def _coerce_scoring_vector(value: Any) -> list[float] | None:
    """Coerce one local vector without letting it poison sibling candidates."""

    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        return None
    vector: list[float] = []
    for component in value:
        if isinstance(component, bool) or not isinstance(
            component,
            (int, float),
        ):
            return None
        number = float(component)
        if not math.isfinite(number):
            return None
        vector.append(number)
    if not vector or len(vector) > 65_536:
        return None
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
    rights: Sequence[Any],
) -> list[float]:
    """Score one query against a batch with one query-norm calculation.

    Invalid candidates receive the legacy ``-1.0`` sentinel in their own
    output slot; valid siblings continue through the same scalar arithmetic.
    """

    query = _coerce_scoring_vector(left)
    if query is None:
        return [-1.0 for _right in rights]
    left_norm = _vector_norm(query)
    if left_norm is None:
        return [-1.0 for _right in rights]
    scores: list[float] = []
    for raw_right in rights:
        right = _coerce_scoring_vector(raw_right)
        if right is None or len(query) != len(right):
            scores.append(-1.0)
            continue
        try:
            dot = sum(a * b for a, b in zip(query, right))
        except (OverflowError, TypeError, ValueError):
            scores.append(-1.0)
            continue
        right_norm = _vector_norm(right)
        if right_norm is None:
            scores.append(-1.0)
            continue
        try:
            score = dot / (left_norm * right_norm)
        except (OverflowError, ZeroDivisionError):
            scores.append(-1.0)
            continue
        scores.append(score if math.isfinite(score) else -1.0)
    return scores


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Scalar compatibility wrapper around the batched implementation."""

    return _cosine_many(left, [right])[0]


def _craft_string_list(value: Any, field: str, *, minimum: int = 1) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) < minimum
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise StateRagError(f"craft catalog {field} must contain at least {minimum} strings")
    return [item.strip() for item in value]


def _load_craft_catalog() -> dict[str, Any]:
    try:
        payload = json.loads(CRAFT_CATALOG_PATH.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise StateRagError(f"craft method catalog is missing: {CRAFT_CATALOG_PATH}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise StateRagError(f"cannot load craft method catalog: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload["version"] != 1
    ):
        raise StateRagError("craft method catalog has an unsupported schema version")
    protocol = _craft_string_list(payload.get("application_protocol"), "application_protocol")
    sources = payload.get("derived_from")
    if not isinstance(sources, list) or not sources:
        raise StateRagError("craft method catalog derived_from must be a non-empty array")
    source_records: list[dict[str, str]] = []
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise StateRagError(f"craft catalog derived_from[{index}] must be an object")
        path = str(source.get("path") or "").strip()
        digest = str(source.get("sha256") or "").strip().lower()
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            raise StateRagError(f"craft catalog derived_from[{index}].path is invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise StateRagError(f"craft catalog derived_from[{index}].sha256 is invalid")
        source_records.append({"path": path, "sha256": digest})
    methods = payload.get("methods")
    if not isinstance(methods, list) or not methods:
        raise StateRagError("craft method catalog methods must be a non-empty array")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(methods):
        if not isinstance(raw, dict):
            raise StateRagError(f"craft catalog methods[{index}] must be an object")
        method_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        principle = str(raw.get("principle") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", method_id) or method_id in seen:
            raise StateRagError(f"craft catalog method id is invalid or duplicated: {method_id!r}")
        if not name or not principle:
            raise StateRagError(f"craft catalog method {method_id} requires name and principle")
        tasks = _craft_string_list(raw.get("tasks"), f"method {method_id}.tasks")
        unknown_tasks = sorted(set(tasks) - CRAFT_TASKS)
        if unknown_tasks:
            raise StateRagError(
                f"craft catalog method {method_id} has unsupported tasks: {', '.join(unknown_tasks)}"
            )
        signals = _craft_string_list(raw.get("signals"), f"method {method_id}.signals")
        steps = _craft_string_list(raw.get("steps"), f"method {method_id}.steps", minimum=2)
        checks = _craft_string_list(raw.get("checks"), f"method {method_id}.checks", minimum=2)
        avoid = _craft_string_list(raw.get("avoid"), f"method {method_id}.avoid")
        priority = raw.get("priority", 0.5)
        if isinstance(priority, bool) or not isinstance(priority, (int, float)):
            raise StateRagError(f"craft catalog method {method_id}.priority must be numeric")
        priority_value = float(priority)
        if not math.isfinite(priority_value) or not 0.0 <= priority_value <= 1.0:
            raise StateRagError(f"craft catalog method {method_id}.priority must be between 0 and 1")
        source = raw.get("source")
        if not isinstance(source, dict):
            raise StateRagError(f"craft catalog method {method_id}.source must be an object")
        source_path = str(source.get("path") or "").strip()
        source_section = str(source.get("section") or "").strip()
        if not source_path or not source_section:
            raise StateRagError(f"craft catalog method {method_id}.source is incomplete")
        seen.add(method_id)
        validated.append(
            {
                "id": method_id,
                "name": name,
                "tasks": tasks,
                "signals": signals,
                "priority": priority_value,
                "principle": principle,
                "steps": steps,
                "checks": checks,
                "avoid": avoid,
                "source": {"path": source_path, "section": source_section},
            }
        )
    return {
        "version": 1,
        "name": str(payload.get("name") or "剧情设计方法卡"),
        "derived_from": source_records,
        "application_protocol": protocol,
        "methods": validated,
    }


def _detect_craft_tasks(prompt: str) -> list[str]:
    text = re.sub(r"\s+", "", str(prompt or ""))
    detected = [task for task, pattern in _CRAFT_TASK_PATTERNS.items() if pattern.search(text)]
    return detected or ["continuation"]


def _craft_document(method: dict[str, Any]) -> str:
    return "\n".join(
        [
            method["name"],
            "适用任务: " + " ".join(method["tasks"]),
            "适用信号: " + " ".join(method["signals"]),
            "原理: " + method["principle"],
            "操作: " + " ".join(method["steps"]),
            "验收: " + " ".join(method["checks"]),
            "避免: " + " ".join(method["avoid"]),
        ]
    )


def _craft_remote_status(config: RuntimeConfig) -> dict[str, Any]:
    remote = {
        "embedding": _service_readiness(config.embedding),
        "rerank": _service_readiness(config.rerank),
    }
    remote["status"] = _remote_overall(remote)
    return remote


def _craft_failure(service: ServiceConfig, reason: str) -> dict[str, Any]:
    failure = _service_readiness(service)
    failure.update({"status": "failed", "reason": reason})
    return failure


def _craft_public_method(candidate: dict[str, Any], detected_tasks: Sequence[str]) -> dict[str, Any]:
    method = candidate["method"]
    task_matches = [task for task in detected_tasks if task in method["tasks"]]
    signal_matches = candidate.get("signal_matches") or []
    reasons: list[str] = []
    if task_matches:
        reasons.append("任务匹配=" + ",".join(task_matches))
    if signal_matches:
        reasons.append("提示信号=" + ",".join(signal_matches[:3]))
    if candidate.get("semantic", -1.0) >= 0.55:
        reasons.append("语义召回")
    if candidate.get("rerank_rank") is not None:
        reasons.append(f"Rerank#{int(candidate['rerank_rank']) + 1}")
    return {
        "id": method["id"],
        "name": method["name"],
        "tasks": method["tasks"],
        "principle": method["principle"],
        "steps": method["steps"],
        "checks": method["checks"],
        "avoid": method["avoid"],
        "source": method["source"],
        "score": round(float(candidate["score"]), 6),
        "why_selected": "；".join(reasons) or "本地方法优先级",
    }


def _format_craft_context(result: dict[str, Any], max_chars: int) -> str:
    methods = result.get("methods") or []
    lines = [
        "[CRAFT_RAG_GUIDANCE]",
        f"retrieval_status: {result.get('status', 'failed')}",
        "detected_tasks: " + ", ".join(result.get("detected_tasks") or ["continuation"]),
        "使用契约（内部执行，默认不要在用户可见正文/大纲中复述方法名）：",
    ]
    for rule in result.get("application_protocol") or []:
        lines.append("- " + str(rule))
    rendered = "\n".join(lines)
    for method in methods:
        block_lines = [
            f"[CRAFT_METHOD:{method['id']}] {method['name']}",
            f"选择原因: {method['why_selected']}",
            f"原理: {method['principle']}",
            "操作:",
            *(f"{index}. {step}" for index, step in enumerate(method["steps"], 1)),
            "验收: " + "；".join(method["checks"]),
            "避免: " + "；".join(method["avoid"]),
            f"来源: {method['source']['path']}#{method['source']['section']}",
        ]
        candidate = rendered + "\n" + "\n".join(block_lines)
        if len(candidate) > max_chars:
            break
        rendered = candidate
    return rendered[:max_chars]


def _craft_trace(result: dict[str, Any]) -> dict[str, Any]:
    methods = result.get("methods") or []
    return {
        "catalog_version": result.get("catalog_version"),
        "detected_tasks": list(result.get("detected_tasks") or []),
        "method_ids": [str(method.get("id") or "") for method in methods if method.get("id")],
        "sources": [method.get("source") for method in methods if isinstance(method.get("source"), dict)],
    }


def _retrieve_craft(
    config: RuntimeConfig,
    prompt: str,
    top_k: int | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    query = str(prompt or "").strip()
    remote = _craft_remote_status(config)
    if not config.craft.enabled:
        return {
            "status": "disabled",
            "reason": "craft RAG is disabled",
            "catalog_version": None,
            "detected_tasks": [],
            "methods": [],
            "methods_count": 0,
            "application_protocol": [],
            "context": "[CRAFT_RAG_GUIDANCE]\nretrieval_status: disabled",
            "remote": remote,
        }
    if not force and not config.craft.auto_retrieve:
        return {
            "status": "skipped",
            "reason": "craft auto retrieval is disabled",
            "catalog_version": None,
            "detected_tasks": [],
            "methods": [],
            "methods_count": 0,
            "application_protocol": [],
            "context": "[CRAFT_RAG_GUIDANCE]\nretrieval_status: skipped",
            "remote": remote,
        }
    if not query:
        raise StateRagError("craft query is empty")
    catalog = _load_craft_catalog()
    detected_tasks = _detect_craft_tasks(query)
    limit = _bounded_int(top_k, config.craft.top_k, 1, 8, "craft.top_k")
    candidates: list[dict[str, Any]] = []
    query_folded = query.casefold()
    for method in catalog["methods"]:
        document = _craft_document(method)
        shared_tasks = [task for task in detected_tasks if task in method["tasks"]]
        signal_matches = [signal for signal in method["signals"] if signal.casefold() in query_folded]
        task_score = min(1.0, len(shared_tasks) / max(1, min(2, len(detected_tasks))))
        signal_score = min(1.0, len(signal_matches) / 2.0)
        lexical = _lexical_score(query, document)
        base = (
            0.46 * task_score
            + 0.26 * lexical
            + 0.18 * signal_score
            + 0.10 * float(method["priority"])
        )
        candidates.append(
            {
                "method": method,
                "document": document,
                "task_score": task_score,
                "signal_matches": signal_matches,
                "lexical": lexical,
                "semantic": -1.0,
                "score": base,
                "rerank_rank": None,
            }
        )
    candidates.sort(key=lambda item: (item["score"], item["method"]["priority"]), reverse=True)
    candidates = candidates[: config.craft.candidate_pool]

    if config.craft.use_embedding:
        try:
            vectors, status = _embedding_call(
                config.embedding,
                [query, *(candidate["document"] for candidate in candidates)],
            )
            remote["embedding"] = status
            query_vector = vectors[0]
            semantic_scores = _cosine_many(query_vector, vectors[1:])
            for candidate, cosine_score in zip(
                candidates,
                semantic_scores,
            ):
                semantic = max(
                    0.0,
                    min(1.0, (cosine_score + 1.0) / 2.0),
                )
                candidate["semantic"] = semantic
                candidate["score"] = 0.56 * candidate["score"] + 0.44 * semantic
        except StateRagError as exc:
            remote["embedding"] = _craft_failure(config.embedding, str(exc))
    else:
        remote["embedding"] = {"status": "skipped", "reason": "craft.use_embedding=false"}
    candidates.sort(key=lambda item: (item["score"], item["method"]["priority"]), reverse=True)

    if config.craft.use_rerank and candidates:
        try:
            ranked, status = _rerank_call(
                config.rerank,
                query,
                [candidate["document"] for candidate in candidates],
                len(candidates),
            )
            remote["rerank"] = status
            total = max(1, len(ranked))
            for rank, (index, raw_score) in enumerate(ranked):
                rank_score = 1.0 - rank / total
                candidates[index]["rerank_rank"] = rank
                candidates[index]["rerank_score"] = raw_score
                candidates[index]["score"] = 0.28 * candidates[index]["score"] + 0.72 * rank_score
            candidates.sort(
                key=lambda item: (
                    item["rerank_rank"] is not None,
                    item["score"],
                    item["method"]["priority"],
                ),
                reverse=True,
            )
        except StateRagError as exc:
            remote["rerank"] = _craft_failure(config.rerank, str(exc))
    else:
        remote["rerank"] = {"status": "skipped", "reason": "craft.use_rerank=false"}
    remote["status"] = _remote_overall(remote)
    status = "degraded" if remote["status"] in {"degraded", "disabled"} else "ready"
    selected = [_craft_public_method(candidate, detected_tasks) for candidate in candidates[:limit]]
    result = {
        "status": status,
        "catalog_version": catalog["version"],
        "catalog_path": str(CRAFT_CATALOG_PATH),
        "source_count": len(catalog["derived_from"]),
        "detected_tasks": detected_tasks,
        "methods": selected,
        "methods_count": len(selected),
        "application_protocol": catalog["application_protocol"],
        "remote": remote,
    }
    result["context"] = _format_craft_context(result, config.craft.max_context_chars)
    return result


def _normalize_categories(config: RuntimeConfig, categories: Any) -> tuple[str, ...]:
    if categories is None:
        return config.categories
    if isinstance(categories, str):
        values = [categories]
    elif isinstance(categories, Sequence):
        values = list(categories)
    else:
        raise StateRagError("categories must be a string array")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or value not in config.categories:
            raise StateRagError(f"unsupported or disabled category: {value!r}")
        if value not in result:
            result.append(value)
    if not result:
        raise StateRagError("categories must not be empty")
    return tuple(result)


def _counts(connection: sqlite3.Connection) -> tuple[int, int]:
    facts = int(connection.execute("SELECT COUNT(*) FROM current_facts").fetchone()[0])
    events = int(connection.execute("SELECT COUNT(*) FROM state_events").fetchone()[0])
    return facts, events


def _retrieve(
    config: RuntimeConfig,
    query: str,
    categories: tuple[str, ...],
    top_k: int,
) -> dict[str, Any]:
    remote = _default_remote_status(config)
    with _open_database(config) as connection:
        placeholders = ",".join("?" for _ in categories)
        rows = list(
            connection.execute(
                f"""
                SELECT f.*, e.scope, e.effective_at, v.model AS vector_model, v.vector_json
                FROM current_facts AS f
                JOIN state_events AS e ON e.event_id = f.event_id
                LEFT JOIN fact_vectors AS v ON v.fact_key = f.fact_key
                WHERE f.category IN ({placeholders})
                ORDER BY f.updated_at DESC, f.fact_key
                LIMIT ?
                """,
                (*categories, MAX_FACTS_SCANNED),
            )
        )
        total_facts, total_events = _counts(connection)
        scoped_rows = list(
            connection.execute(
                f"""
                SELECT * FROM state_events
                WHERE category IN ({placeholders}) AND scope IN ('planned', 'historical')
                ORDER BY created_at DESC, event_id DESC
                LIMIT 2000
                """,
                categories,
            )
        )

    candidates: list[dict[str, Any]] = []
    for recency, row in enumerate(rows):
        fact = _public_fact(row)
        document = _render_fact(fact) + "\nEvidence: " + fact["evidence"]
        lexical = _lexical_score(query, document) if query.strip() else 1.0 / (recency + 1)
        normalized_query = query.casefold().strip()
        exact_forms = {
            fact["subject"].casefold(),
            fact["field"].casefold(),
            f"{fact['subject']}.{fact['field']}".casefold(),
            f"{fact['subject']} {fact['field']}".casefold(),
            f"{fact['category']}:{fact['subject']}.{fact['field']}".casefold(),
        }
        exact = bool(normalized_query and normalized_query in exact_forms)
        if exact:
            lexical = 2.0
        candidates.append(
            {
                "row": row,
                "fact": fact,
                "document": document,
                "lexical": lexical,
                "score": lexical,
                "exact": exact,
            }
        )
    scoped_seen: set[tuple[str, str, str, str]] = set()
    for recency, event in enumerate(scoped_rows, start=len(rows)):
        identity = (
            str(event["scope"]),
            str(event["category"]),
            str(event["subject"]).casefold(),
            str(event["field"]).casefold(),
        )
        if identity in scoped_seen:
            continue
        scoped_seen.add(identity)
        if str(event["operation"]) == "delete":
            continue
        row = {
            "fact_key": f"{event['scope']}-" + _fact_key(
                str(event["category"]), str(event["subject"]), str(event["field"])
            ),
            "category": event["category"],
            "subject": event["subject"],
            "field": event["field"],
            "scope": event["scope"],
            "effective_at": event["effective_at"],
            "value_json": event["value_json"],
            "event_id": event["event_id"],
            "confidence": event["confidence"],
            "evidence": event["evidence"],
            "updated_at": event["created_at"],
            "vector_model": None,
            "vector_json": None,
        }
        fact = _public_fact(row)
        document = _render_fact(fact) + "\nEvidence: " + fact["evidence"]
        lexical = _lexical_score(query, document) if query.strip() else 1.0 / (recency + 1)
        candidates.append(
            {
                "row": row,
                "fact": fact,
                "document": document,
                "lexical": lexical,
                "score": lexical,
                "exact": False,
            }
        )

    query_vector: list[float] | None = None
    if config.embedding.enabled and candidates and query.strip():
        try:
            vectors, status = _embedding_call(config.embedding, [query])
            remote["embedding"] = status
            query_vector = vectors[0]
        except StateRagError as exc:
            failed = _service_readiness(config.embedding)
            failed.update({"status": "failed", "reason": str(exc)})
            remote["embedding"] = failed
    stored_vectors: list[Any] = []
    for candidate in candidates:
        row = candidate["row"]
        stored: Any = None
        if (
            query_vector is not None
            and row["vector_json"]
            and row["vector_model"] == config.embedding.model
        ):
            try:
                stored = json.loads(str(row["vector_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                stored = None
        stored_vectors.append(stored)
    vector_scores = (
        _cosine_many(query_vector, stored_vectors)
        if query_vector is not None
        else [-1.0 for _candidate in candidates]
    )
    for candidate, vector_score in zip(candidates, vector_scores):
        candidate["vector"] = vector_score
        if vector_score >= -0.5:
            candidate["score"] = 0.65 * candidate["lexical"] + 0.35 * ((vector_score + 1.0) / 2.0)

    candidates.sort(key=lambda item: (item["score"], item["fact"]["updated_at"]), reverse=True)
    exact_pool = [item for item in candidates if item.get("exact")]
    semantic_pool = [item for item in candidates if not item.get("exact")][
        : max(top_k * 4, 24)
    ]
    pool = exact_pool + semantic_pool
    if config.rerank.enabled and semantic_pool and query.strip():
        try:
            ranked, status = _rerank_call(
                config.rerank,
                query,
                [item["document"] for item in semantic_pool],
                min(top_k, len(semantic_pool)),
            )
            remote["rerank"] = status
            pool = exact_pool + [dict(semantic_pool[index], score=score) for index, score in ranked]
        except StateRagError as exc:
            failed = _service_readiness(config.rerank)
            failed.update({"status": "failed", "reason": str(exc)})
            remote["rerank"] = failed

    selected = pool[:top_k]
    facts = [_public_fact(item["row"], item["score"]) for item in selected]
    remote["status"] = _remote_overall(remote)
    degraded = any(
        remote[name].get("status") == "failed" for name in ("embedding", "rerank")
    )
    # With no configured remote service, local retrieval still works, but an
    # empty result must not masquerade as authoritative semantic absence.
    no_remote = all(
        remote[name].get("status") in {"disabled", "unconfigured"}
        for name in ("embedding", "rerank", "extract")
    )
    status = "degraded" if degraded or no_remote else "ok"
    return {
        "status": status,
        "query": query,
        "categories": list(categories),
        "facts": facts,
        "facts_count": len(facts),
        "total_facts_count": total_facts,
        "events_count": total_events,
        "absence_confirmed": bool(not facts and not degraded and not no_remote),
        "remote": remote,
    }


def _request_identity(
    request_id: str, session_id: str, turn_id: str, prompt: str, assistant_text: str = ""
) -> tuple[str, str]:
    cleaned = str(request_id or "").strip()
    if not cleaned:
        basis = (session_id, turn_id, prompt) if prompt or turn_id else (session_id, assistant_text)
        cleaned = "srq-" + _hash("state-rag-request", *basis, length=24)
    receipt = "srr-" + _hash("state-rag-receipt", cleaned, length=24)
    return cleaned, receipt


def _authority_preflight(root: Path, prompt: str, request_id: str) -> dict[str, Any]:
    try:
        result = query_project(root, prompt, request_id=request_id)
    except Exception as exc:  # Defensive: the sibling API normally returns an error object.
        return {
            "status": "INDEX_UNAVAILABLE",
            "reason": str(exc),
            "request_id": request_id,
            "evidence": [],
        }
    return result if isinstance(result, dict) else {
        "status": "INDEX_UNAVAILABLE",
        "reason": "plot_rag.query_project returned a non-object",
        "request_id": request_id,
        "evidence": [],
    }


def _format_context(
    receipt_id: str,
    request_id: str,
    authority: dict[str, Any],
    facts: Sequence[dict[str, Any]],
    craft: dict[str, Any],
    max_chars: int,
) -> str:
    lines = [
        "[STATE_RAG_RECEIPT]",
        f"receipt_id: {receipt_id}",
        f"request_id: {request_id}",
        f"authority_status: {authority.get('status', 'INDEX_UNAVAILABLE')}",
        "[AUTHORITATIVE_PREFLIGHT]",
    ]
    passages = authority.get("evidence") or authority.get("candidates") or []
    if isinstance(passages, list):
        for passage in passages:
            if not isinstance(passage, dict):
                continue
            path = passage.get("path", passage.get("source", ""))
            absolute_path = passage.get("absolute_path", "")
            start = passage.get("start_line", "?")
            end = passage.get("end_line", "?")
            text = passage.get(
                "excerpt", passage.get("text", passage.get("passage", ""))
            )
            source = str(path)
            if absolute_path and str(absolute_path) != source:
                source += f" ({absolute_path})"
            lines.append(f"- {source}:{start}-{end}\n  {str(text).strip()}")
    if len(lines) == 5:
        lines.append("- No decisive passage from this one-query broad preflight.")
    lines.append("[CURRENT_STORY_STATE]")
    if facts:
        lines.extend("- " + _render_fact(fact) + f" | evidence: {fact['evidence']}" for fact in facts)
    else:
        lines.append("- No current fact was retrieved; this is not proof that the fact is absent.")
    lines.extend(
        [
            "[STATE_RAG_RULE]",
            "Treat INDEX_UNAVAILABLE, AMBIGUOUS, degraded retrieval, and a single empty preflight as unknown, not as factual absence.",
        ]
    )
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 32)].rstrip() + "\n[context truncated]"
    craft_context = str(craft.get("context") or "").strip()
    return text if not craft_context else text + "\n" + craft_context


def _failed_result(
    *,
    request_id: str = "",
    receipt_id: str = "",
    reason: str,
    remote: dict[str, Any] | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "failed",
        "request_id": request_id,
        "receipt_id": receipt_id,
        "receipt": receipt_id,
        "reason": reason,
        "remote": remote or _default_remote_status(),
        "facts": [],
        "facts_count": 0,
        "events_count": 0,
    }
    if commit:
        result.update({"recorded_events": [], "updated_facts": [], "skipped": []})
    return result


def query_state(
    project_root: Path | str,
    query: str,
    categories: Sequence[str] | str | None = None,
    top_k: int | None = None,
) -> dict[str, Any]:
    """Retrieve current projected facts with lexical, vector, and rerank signals."""

    try:
        config = _load_runtime_config(project_root)
        if not bool(getattr(config, "enabled", True)):
            result = _failed_result(reason="state RAG is disabled", remote=_default_remote_status(config))
            result["status"] = "disabled"
            return result
        selected_categories = _normalize_categories(config, categories)
        limit = _bounded_int(top_k, config.top_k, 1, 100, "query.top_k")
        result = _retrieve(config, str(query or "").strip(), selected_categories, limit)
        result.update({"request_id": "", "receipt_id": "", "receipt": ""})
        return result
    except (StateRagError, PlotRagError, OSError, sqlite3.Error) as exc:
        return _failed_result(reason=str(exc))


def query_craft(
    project_root: Path | str,
    query: str,
    top_k: int | None = None,
) -> dict[str, Any]:
    """Retrieve task-relevant plot-design methods from the bundled guide-derived catalog."""

    try:
        config = _load_runtime_config(project_root)
        if not config.enabled:
            result = _failed_result(reason="plot RAG is disabled", remote=_default_remote_status(config))
            result["status"] = "disabled"
            return result
        result = _retrieve_craft(config, str(query or "").strip(), top_k, force=True)
        result.update({"request_id": "", "receipt_id": "", "receipt": ""})
        return result
    except (StateRagError, PlotRagError, OSError, json.JSONDecodeError) as exc:
        return _failed_result(reason=str(exc))


def prepare_turn(
    project_root: Path | str,
    prompt: str,
    request_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    *,
    authority_preflight: bool = True,
) -> dict[str, Any]:
    """Retrieve context and persist an idempotent pending receipt for one turn."""

    prompt = str(prompt or "").strip()
    rid, receipt_id = _request_identity(request_id, session_id, turn_id, prompt)
    if not prompt:
        return _failed_result(request_id=rid, receipt_id=receipt_id, reason="prompt is empty")
    if type(authority_preflight) is not bool:
        return _failed_result(
            request_id=rid,
            receipt_id=receipt_id,
            reason="authority_preflight must be boolean",
        )
    try:
        config = _load_runtime_config(project_root)
        remote = _default_remote_status(config)
        if not config.enabled:
            result = _failed_result(
                request_id=rid, receipt_id=receipt_id, reason="state RAG is disabled", remote=remote
            )
            result["status"] = "disabled"
            return result

        if config.auto_retrieve:
            retrieval = _retrieve(config, prompt, config.categories, config.top_k)
        else:
            with _open_database(config) as connection:
                total_facts, total_events = _counts(connection)
            retrieval = {
                "status": "skipped",
                "facts": [],
                "facts_count": 0,
                "total_facts_count": total_facts,
                "events_count": total_events,
                "absence_confirmed": False,
                "remote": remote,
            }
        remote = retrieval["remote"]
        craft = _retrieve_craft(config, prompt, config.craft.top_k)
        remote["craft"] = craft["remote"]
        authority = (
            _authority_preflight(config.root, prompt, rid)
            if authority_preflight
            else {
                "status": "DEFERRED_TO_LONGFORM",
                "reason": (
                    "strict lifecycle authority retrieval is assembled by "
                    "the accepted longform context"
                ),
                "request_id": rid,
                "evidence": [],
                "absence_confirmed": False,
            }
        )
        context = _format_context(
            receipt_id,
            rid,
            authority,
            retrieval["facts"],
            craft,
            config.max_context_chars,
        )
        authority_degraded = authority.get("status") == "INDEX_UNAVAILABLE"
        extraction_unready = config.auto_record and not bool(
            remote["extract"].get("configured")
        )
        craft_degraded = craft.get("status") in {"degraded", "failed", "error"}
        status = "degraded" if (
            retrieval["status"] == "degraded"
            or authority_degraded
            or extraction_unready
            or craft_degraded
        ) else "ready"
        now = _utc_now()
        prompt_hash = _hash(prompt, length=64)
        with _open_database(config) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM turns WHERE request_id = ?", (rid,)
            ).fetchone()
            if existing is not None and str(existing["prompt_hash"]) != prompt_hash:
                connection.rollback()
                return _failed_result(
                    request_id=rid,
                    receipt_id=str(existing["receipt_id"]),
                    reason="request_id is already bound to a different prompt",
                    remote=remote,
                )
            if existing is not None and str(existing["status"]) == "committed":
                connection.rollback()
                stored = json.loads(str(existing["result_json"] or "{}"))
                stored.setdefault("status", "committed")
                stored["context"] = context
                stored["facts"] = retrieval["facts"]
                stored["facts_count"] = retrieval["facts_count"]
                stored["events_count"] = retrieval["events_count"]
                stored["craft"] = craft
                return stored
            connection.execute(
                """
                INSERT INTO turns(
                    receipt_id, request_id, session_id, turn_id, prompt, prompt_hash,
                    status, retrieved_json, authority_json, craft_json, remote_json, started_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    turn_id=excluded.turn_id,
                    status='pending',
                    retrieved_json=excluded.retrieved_json,
                    authority_json=excluded.authority_json,
                    craft_json=excluded.craft_json,
                    remote_json=excluded.remote_json,
                    error='',
                    started_at=excluded.started_at,
                    completed_at=NULL
                """,
                (
                    receipt_id,
                    rid,
                    str(session_id or ""),
                    str(turn_id or ""),
                    prompt,
                    prompt_hash,
                    _json_dumps(retrieval["facts"]),
                    _json_dumps(authority),
                    _json_dumps(craft),
                    _json_dumps(remote),
                    now,
                ),
            )
            connection.commit()
        return {
            "status": status,
            "request_id": rid,
            "receipt_id": receipt_id,
            "receipt": receipt_id,
            "context": context,
            "facts": retrieval["facts"],
            "facts_count": retrieval["facts_count"],
            "total_facts_count": retrieval["total_facts_count"],
            "events_count": retrieval["events_count"],
            "absence_confirmed": False if status == "degraded" else retrieval["absence_confirmed"],
            "remote": remote,
            "authority": authority,
            "craft": craft,
            "turn_status": "pending",
        }
    except (StateRagError, PlotRagError, OSError, sqlite3.Error) as exc:
        return _failed_result(request_id=rid, receipt_id=receipt_id, reason=str(exc))


def _find_turn(
    connection: sqlite3.Connection,
    request_id: str,
    session_id: str,
    turn_id: str = "",
) -> sqlite3.Row | None:
    if request_id:
        row = connection.execute(
            "SELECT * FROM turns WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT * FROM turns WHERE receipt_id = ?", (request_id,)
            ).fetchone()
        return row
    if session_id:
        if not turn_id:
            return None
        return connection.execute(
            """
            SELECT * FROM turns
            WHERE session_id = ? AND turn_id = ? AND status IN ('pending', 'failed')
            ORDER BY started_at DESC LIMIT 1
            """,
            (session_id, turn_id),
        ).fetchone()
    return None


def _mark_turn_failed(
    config: RuntimeConfig,
    request_id: str,
    receipt_id: str,
    reason: str,
    remote: dict[str, Any],
) -> None:
    with _open_database(config) as connection:
        connection.execute(
            """
            UPDATE turns SET status='failed', error=?, remote_json=?, completed_at=?
            WHERE (request_id=? OR receipt_id=?) AND status <> 'committed'
            """,
            (reason, _json_dumps(remote), _utc_now(), request_id, receipt_id),
        )
        connection.commit()


def _fact_documents(deltas: Sequence[dict[str, Any]]) -> tuple[list[str], list[str]]:
    keys: list[str] = []
    documents: list[str] = []
    seen: set[str] = set()
    for delta in deltas:
        if delta["operation"] != "set" or delta.get("scope", "current") != "current":
            continue
        key = _fact_key(delta["category"], delta["subject"], delta["field"])
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
        documents.append(
            f"[{delta['category']}] {delta['subject']}.{delta['field']} = {_render_value(delta['value'])}\n"
            f"Evidence: {delta['evidence']}"
        )
    return keys, documents


def _snapshot_payload(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = list(connection.execute("SELECT * FROM current_facts ORDER BY category, subject, field"))
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _utc_now(),
        "facts": [_public_fact(row) for row in rows],
    }


def _write_snapshot_payload(
    config: RuntimeConfig,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    published = dict(payload)
    target = config.snapshot_path.resolve()
    if not _is_inside(target, config.root):
        raise StateRagError("snapshot path resolves outside the project")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(target.parent),
            prefix=".state_snapshot.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(
                published,
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return published


def _write_snapshot(config: RuntimeConfig) -> dict[str, Any]:
    with _open_database(config) as connection:
        payload = _snapshot_payload(connection)
    return _write_snapshot_payload(config, payload)


def _continuity_entity_entry(
    entity_id: Any,
    entity_catalog: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    if entity_id is None:
        return {}
    entry = entity_catalog.get(str(entity_id))
    return entry if isinstance(entry, Mapping) else {}


def _continuity_entity_label(
    entity_id: Any,
    entity_catalog: Mapping[str, Mapping[str, Any]],
) -> str:
    if entity_id is None:
        return ""
    text = str(entity_id).strip()
    if not text:
        return ""
    entry = _continuity_entity_entry(text, entity_catalog)
    return str(entry.get("canonical_name") or text)


def _continuity_fact_category(
    fact: Mapping[str, Any],
    entity_catalog: Mapping[str, Mapping[str, Any]],
) -> str:
    fact_type = str(fact.get("fact_type") or "fact")
    direct = _CONTINUITY_FACT_CATEGORIES.get(fact_type)
    if direct is not None:
        return direct
    for prefix, category in (
        ("ability_", "ability"),
        ("progression_", "progression"),
        ("resource_", "resource"),
        ("status_effect_", "status"),
        ("power_binding_", "binding"),
        ("qualification_", "qualification"),
        ("power_observation_", "observation"),
    ):
        if fact_type.startswith(prefix):
            return category
    if fact_type in {"state", "goal", "injury", "commitment"}:
        entity_id = (
            fact.get("subject_entity_id")
            or fact.get("entity_id")
            or fact.get("target_entity_id")
        )
        entity_type = str(
            _continuity_entity_entry(entity_id, entity_catalog).get(
                "entity_type"
            )
            or ""
        )
        if not entity_type or entity_type in {
            "character",
            "actor",
            "summon",
        }:
            return "character_state"
    return "world_state"


def _legacy_fact_from_continuity(
    fact: Mapping[str, Any],
    *,
    entity_catalog: Mapping[str, Mapping[str, Any]],
    updated_at: str,
) -> dict[str, Any]:
    category = _continuity_fact_category(fact, entity_catalog)
    entity_id = fact.get("entity_id")
    subject_entity_id = fact.get("subject_entity_id")
    target_entity_id = fact.get("target_entity_id")
    subject_id = subject_entity_id or entity_id or target_entity_id
    subject = _continuity_entity_label(subject_id, entity_catalog)
    if not subject:
        subject = "故事" if category == "story_time" else "世界"

    field = str(fact.get("field") or fact.get("fact_type") or "value")
    value = fact.get("value")
    if category == "relationship":
        target = _continuity_entity_label(
            target_entity_id,
            entity_catalog,
        )
        if isinstance(value, Mapping):
            relation = dict(value)
        else:
            relation = {"value": value}
        relation["dimension"] = field
        relation["target"] = target
        value = relation
    elif category == "location":
        field = "current"
        target = _continuity_entity_label(
            target_entity_id,
            entity_catalog,
        )
        if target:
            value = target
    elif category == "inventory":
        item = _continuity_entity_label(entity_id, entity_catalog)
        if item:
            field = f"item:{item}"
        inventory = dict(value) if isinstance(value, Mapping) else {}
        inventory.setdefault("item", item)
        owner = _continuity_entity_label(
            subject_entity_id,
            entity_catalog,
        )
        if owner:
            inventory.setdefault("owner", owner)
        value = inventory or value
    elif category == "story_time":
        field = "current"

    source_event_id = str(fact.get("source_event_id") or "")
    scope = str(fact.get("scope") or "current")
    source_fact_key = str(fact.get("fact_key") or "").strip()
    if not source_fact_key:
        source_fact_key = _hash(
            fact.get("fact_type"),
            entity_id,
            subject_entity_id,
            target_entity_id,
            field,
            length=40,
        )
    return {
        "fact_key": _hash(
            "continuity_snapshot_v1",
            scope,
            source_fact_key,
            fact.get("fact_type"),
            entity_id,
            subject_entity_id,
            target_entity_id,
            field,
            length=40,
        ),
        "category": category,
        "subject": subject,
        "field": field,
        "scope": scope,
        "effective_at": fact.get("story_time"),
        "value": value,
        "confidence": 1.0,
        "evidence": (
            f"accepted continuity event {source_event_id}"
            if source_event_id
            else "accepted continuity projection"
        ),
        "event_id": source_event_id,
        "updated_at": updated_at,
    }


def _continuity_snapshot_payload(
    facts: Sequence[Mapping[str, Any]],
    *,
    entity_catalog: Mapping[str, Mapping[str, Any]],
    updated_at: str,
) -> dict[str, Any]:
    projected: list[dict[str, Any]] = []
    for index, fact in enumerate(facts):
        if not isinstance(fact, Mapping):
            raise StateRagError(
                f"continuity_facts[{index}] must be an object"
            )
        scope = str(fact.get("scope") or "current")
        if scope not in {"current", "timeless"} or bool(
            fact.get("provisional")
        ):
            continue
        projected.append(
            _legacy_fact_from_continuity(
                fact,
                entity_catalog=entity_catalog,
                updated_at=updated_at,
            )
        )
    projected.sort(
        key=lambda fact: (
            fact["category"],
            fact["subject"],
            fact["field"],
            fact["scope"],
            fact["fact_key"],
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": updated_at,
        "facts": projected,
    }


def rebuild_state_snapshot(
    project_root: Path | str,
    *,
    continuity_facts: Sequence[Mapping[str, Any]] | None = None,
    entity_catalog: Mapping[str, Mapping[str, Any]] | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Rebuild the legacy-shaped snapshot from the selected authority surface.

    Config v1/v2 keeps the original ``current_facts`` projection. Config v3
    callers must pass the accepted facts returned by the same continuity query
    used for ``continuity_snapshot.json``; this prevents replay from publishing
    a stale legacy projection after retraction or artifact supersession.
    """

    config = _load_runtime_config(project_root)
    authority = "legacy_current_facts"
    if continuity_facts is None:
        if config.version >= 3:
            raise StateRagError(
                "config v3 snapshot rebuild requires authoritative continuity facts"
            )
        payload = _write_snapshot(config)
    else:
        authority = "continuity_v5"
        snapshot_time = str(updated_at or _utc_now())
        payload = _continuity_snapshot_payload(
            continuity_facts,
            entity_catalog=entity_catalog or {},
            updated_at=snapshot_time,
        )
        payload = _write_snapshot_payload(config, payload)
    return {
        "status": "completed",
        "path": str(config.snapshot_path.resolve()),
        "schema_version": payload["schema_version"],
        "facts_count": len(payload["facts"]),
        "updated_at": payload["updated_at"],
        "authority": authority,
    }


def _commit_artifact_path(config: RuntimeConfig, request_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", request_id):
        filename = request_id + ".json"
    else:
        filename = "request-" + _hash(request_id, length=32) + ".json"
    target = (config.commit_dir / filename).resolve()
    if not _is_inside(target, config.root):
        raise StateRagError("commit artifact path resolves outside the project")
    return target


def _write_immutable_json(target: Path, payload: dict[str, Any]) -> str:
    """Atomically publish an immutable JSON file without replacing an existing one."""

    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    if target.is_file():
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise StateRagError(f"cannot verify existing commit artifact: {exc}") from exc
        if existing != encoded:
            raise StateRagError(f"immutable commit artifact conflict: {target}")
        return "exists"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(target.parent),
            prefix=".commit.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
            status = "created"
        except FileExistsError:
            existing = target.read_bytes()
            if existing != encoded:
                raise StateRagError(f"immutable commit artifact conflict: {target}")
            status = "exists"
        except OSError:
            # Some Windows filesystems do not permit hard links.  Re-check the
            # destination immediately before the atomic replace fallback.
            if target.exists():
                existing = target.read_bytes()
                if existing != encoded:
                    raise StateRagError(f"immutable commit artifact conflict: {target}")
                status = "exists"
            else:
                os.replace(temporary, target)
                temporary = None
                status = "created"
        return status
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def commit_turn(
    project_root: Path | str,
    assistant_text: str,
    request_id: str = "",
    session_id: str = "",
    prompt: str = "",
    turn_id: str = "",
) -> dict[str, Any]:
    """Extract, validate, and atomically commit one assistant turn's state deltas."""

    assistant_text = str(assistant_text or "")
    prompt = str(prompt or "").strip()
    provisional_rid, provisional_receipt = _request_identity(
        request_id, session_id, turn_id, prompt, assistant_text
    )
    if not assistant_text.strip():
        return _failed_result(
            request_id=provisional_rid,
            receipt_id=provisional_receipt,
            reason="assistant_text is empty",
            commit=True,
        )
    incoming_source_hash = _hash(assistant_text, length=64)
    try:
        config = _load_runtime_config(project_root)
        remote = _default_remote_status(config)
        if not config.enabled:
            result = _failed_result(
                request_id=provisional_rid,
                receipt_id=provisional_receipt,
                reason="state RAG is disabled",
                remote=remote,
                commit=True,
            )
            result["status"] = "disabled"
            return result
        # Strict v3 keeps the proposal-only lifecycle in v1_runtime, but the
        # legacy entry point must still honor the local enable/auto-record
        # switches before delegating.  Pass the normalized request identity so
        # prompt-only prepare/commit calls address the same pending receipt.
        strict_lifecycle = False
        if config.version >= 3:
            loaded_config = load_config(config.root)
            strict_lifecycle = bool(
                (loaded_config.get("lifecycle") or {}).get("strict", False)
            )
        if not bool(getattr(config, "auto_record", True)):
            return {
                "status": "skipped",
                "reason": "auto_record_disabled",
                "request_id": provisional_rid,
                "receipt_id": provisional_receipt,
                "receipt": provisional_receipt,
                "recorded_events": [],
                "updated_facts": [],
                "skipped": [{"reason": "auto_record_disabled"}],
                "facts": [],
                "facts_count": 0,
                "events_count": 0,
                "remote": remote,
            }
        if strict_lifecycle:
            if __package__:
                from .v1_runtime import propose_plot_turn
            else:
                from v1_runtime import propose_plot_turn
            return propose_plot_turn(
                config.root,
                assistant_text,
                request_id=provisional_rid,
                session_id=session_id,
                turn_id=turn_id,
                prompt=prompt,
            )
        lookup_request = str(request_id or "").strip()
        if not lookup_request and prompt and not session_id:
            lookup_request, _ = _request_identity("", session_id, "", prompt)
        turn = None
        facts_count = 0
        events_count = 0
        if config.db_path.is_file():
            with _open_readonly_database(config) as connection:
                turn = _find_turn(
                    connection,
                    lookup_request,
                    "" if lookup_request else str(session_id or ""),
                    "" if lookup_request else str(turn_id or ""),
                )
                facts_count, events_count = _counts(connection)
        if turn is None:
            return {
                "status": "skipped",
                "reason": "no_prepared_turn",
                "request_id": provisional_rid,
                "receipt_id": provisional_receipt,
                "receipt": provisional_receipt,
                "recorded_events": [],
                "updated_facts": [],
                "skipped": [{"reason": "no_prepared_turn"}],
                "facts": [],
                "facts_count": facts_count,
                "events_count": events_count,
                "remote": remote,
            }
        if not config.auto_record:
            return {
                "status": "skipped",
                "reason": "auto_record_disabled",
                "request_id": str(turn["request_id"]),
                "receipt_id": str(turn["receipt_id"]),
                "receipt": str(turn["receipt_id"]),
                "recorded_events": [],
                "updated_facts": [],
                "skipped": [{"reason": "auto_record_disabled"}],
                "facts": [],
                "facts_count": facts_count,
                "events_count": events_count,
                "remote": remote,
            }
        with _open_readonly_database(config) as connection:
            if turn is not None and str(turn["status"]) == "committed":
                stored_hash = str(turn["assistant_hash"] or "")
                if stored_hash and stored_hash != incoming_source_hash:
                    conflict = _failed_result(
                        request_id=str(turn["request_id"]),
                        receipt_id=str(turn["receipt_id"]),
                        reason="receipt is already committed with different assistant_text",
                        remote=remote,
                        commit=True,
                    )
                    conflict.update(
                        {"facts_count": facts_count, "events_count": events_count}
                    )
                    return conflict
                stored = json.loads(str(turn["result_json"] or "{}"))
                if isinstance(stored, dict):
                    stored.setdefault("status", "committed")
                    stored["idempotent"] = True
                    return stored
            if str(turn["status"]) not in {"pending", "failed"}:
                return {
                    "status": "skipped",
                    "reason": "no_prepared_turn",
                    "request_id": provisional_rid,
                    "receipt_id": provisional_receipt,
                    "receipt": provisional_receipt,
                    "recorded_events": [],
                    "updated_facts": [],
                    "skipped": [{"reason": "no_prepared_turn"}],
                    "facts": [],
                    "facts_count": facts_count,
                    "events_count": events_count,
                    "remote": remote,
                }
            rid = str(turn["request_id"])
            receipt_id = str(turn["receipt_id"])
            effective_prompt = prompt or str(turn["prompt"])
            if prompt and str(turn["prompt"]) and _hash(prompt, length=64) != str(turn["prompt_hash"]):
                return _failed_result(
                    request_id=rid,
                    receipt_id=receipt_id,
                    reason="prompt does not match the prepared receipt",
                    remote=remote,
                    commit=True,
                )
            try:
                retrieved_facts = json.loads(str(turn["retrieved_json"] or "[]"))
            except json.JSONDecodeError:
                retrieved_facts = []
            try:
                craft_selection = json.loads(str(turn["craft_json"] or "{}"))
            except json.JSONDecodeError:
                craft_selection = {}
            if not isinstance(craft_selection, dict):
                craft_selection = {}
            craft_trace = _craft_trace(craft_selection)

        try:
            deltas, extraction_skipped, extract_status = _chat_extract(
                config, assistant_text, effective_prompt, retrieved_facts
            )
            if config.version >= 3:
                _legacy_deltas, item_candidates = split_delta_v4_results(
                    deltas
                )
                if item_candidates:
                    raise StateRagError(
                        "ITEM_DELTA_V4_REQUIRES_STRICT_PROPOSAL_ADAPTER"
                    )
            remote["extract"] = extract_status
        except StateRagError as exc:
            failed = _service_readiness(config.extract)
            failed.update({"status": "failed", "reason": str(exc)})
            remote["extract"] = failed
            remote["status"] = "degraded"
            _mark_turn_failed(config, rid, receipt_id, str(exc), remote)
            with _open_database(config) as connection:
                facts_count, events_count = _counts(connection)
            result = _failed_result(
                request_id=rid,
                receipt_id=receipt_id,
                reason=str(exc),
                remote=remote,
                commit=True,
            )
            result.update({"facts_count": facts_count, "events_count": events_count})
            return result

        vectors_by_key: dict[str, list[float]] = {}
        if config.embedding.enabled:
            fact_keys, documents = _fact_documents(deltas)
            if documents:
                try:
                    vectors, embedding_status = _embedding_call(config.embedding, documents)
                    vectors_by_key = dict(zip(fact_keys, vectors))
                    remote["embedding"] = embedding_status
                except StateRagError as exc:
                    failed = _service_readiness(config.embedding)
                    failed.update({"status": "failed", "reason": str(exc)})
                    remote["embedding"] = failed
        remote["status"] = _remote_overall(remote)

        source_hash = incoming_source_hash
        now = _utc_now()
        recorded: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = list(extraction_skipped)
        with _open_database(config) as connection:
            connection.execute("BEGIN IMMEDIATE")
            locked_turn = connection.execute(
                "SELECT * FROM turns WHERE request_id=?", (rid,)
            ).fetchone()
            if locked_turn is not None and str(locked_turn["status"]) == "committed":
                connection.rollback()
                stored_hash = str(locked_turn["assistant_hash"] or "")
                if stored_hash and stored_hash != source_hash:
                    return _failed_result(
                        request_id=rid,
                        receipt_id=receipt_id,
                        reason="receipt is already committed with different assistant_text",
                        remote=remote,
                        commit=True,
                    )
                stored = json.loads(str(locked_turn["result_json"] or "{}"))
                stored.setdefault("status", "committed")
                stored["idempotent"] = True
                return stored

            base_revision = int(
                connection.execute("SELECT COUNT(*) FROM state_events").fetchone()[0]
            )

            for index, delta in enumerate(deltas):
                fact_key = _fact_key(delta["category"], delta["subject"], delta["field"])
                event_id = "se-" + _hash(
                    rid, index, source_hash, _json_dumps(delta), length=40
                )
                exists = connection.execute(
                    "SELECT 1 FROM state_events WHERE event_id=?", (event_id,)
                ).fetchone()
                if exists is not None:
                    skipped.append({"event_id": event_id, "reason": "duplicate_event"})
                    continue
                value_json = None if delta["operation"] == "delete" else _json_dumps(delta["value"])
                connection.execute(
                    """
                    INSERT INTO state_events(
                        event_id, request_id, receipt_id, session_id, category, subject,
                        field, operation, scope, effective_at, value_json, confidence,
                        evidence, source_hash, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        rid,
                        receipt_id,
                        str(session_id or ""),
                        delta["category"],
                        delta["subject"],
                        delta["field"],
                        delta["operation"],
                        delta["scope"],
                        None if delta["effective_at"] is None else str(delta["effective_at"]),
                        value_json,
                        delta["confidence"],
                        delta["evidence"],
                        source_hash,
                        now,
                    ),
                )
                event_public = dict(delta, event_id=event_id, request_id=rid)
                recorded.append(event_public)
                if delta["scope"] != "current":
                    updated.append(
                        {
                            "event_id": event_id,
                            "operation": "event_only",
                            "scope": delta["scope"],
                            "category": delta["category"],
                            "subject": delta["subject"],
                            "field": delta["field"],
                        }
                    )
                    continue
                if delta["operation"] == "delete":
                    cursor = connection.execute("DELETE FROM current_facts WHERE fact_key=?", (fact_key,))
                    connection.execute("DELETE FROM fact_vectors WHERE fact_key=?", (fact_key,))
                    updated.append(
                        {"fact_key": fact_key, "operation": "delete", "changed": cursor.rowcount > 0}
                    )
                    continue
                connection.execute(
                    """
                    INSERT INTO current_facts(
                        fact_key, category, subject, field, value_json, event_id,
                        effective_at, confidence, evidence, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(fact_key) DO UPDATE SET
                        category=excluded.category,
                        subject=excluded.subject,
                        field=excluded.field,
                        value_json=excluded.value_json,
                        event_id=excluded.event_id,
                        effective_at=excluded.effective_at,
                        confidence=excluded.confidence,
                        evidence=excluded.evidence,
                        updated_at=excluded.updated_at
                    """,
                    (
                        fact_key,
                        delta["category"],
                        delta["subject"],
                        delta["field"],
                        value_json,
                        event_id,
                        None if delta["effective_at"] is None else str(delta["effective_at"]),
                        delta["confidence"],
                        delta["evidence"],
                        now,
                    ),
                )
                vector = vectors_by_key.get(fact_key)
                if vector is not None:
                    connection.execute(
                        """
                        INSERT INTO fact_vectors(fact_key, model, dimensions, vector_json, updated_at)
                        VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(fact_key) DO UPDATE SET
                            model=excluded.model,
                            dimensions=excluded.dimensions,
                            vector_json=excluded.vector_json,
                            updated_at=excluded.updated_at
                        """,
                        (fact_key, config.embedding.model, len(vector), _json_dumps(vector), now),
                    )
                else:
                    connection.execute("DELETE FROM fact_vectors WHERE fact_key=?", (fact_key,))
                updated.append(
                    {
                        "fact_key": fact_key,
                        "operation": "set",
                        "category": delta["category"],
                        "subject": delta["subject"],
                        "field": delta["field"],
                        "value": delta["value"],
                    }
                )

            facts_count, events_count = _counts(connection)
            status = "degraded" if any(
                remote[name].get("status") == "failed" for name in ("embedding", "rerank")
            ) else "committed"
            result = {
                "status": status,
                "request_id": rid,
                "receipt_id": receipt_id,
                "receipt": receipt_id,
                "recorded_events": recorded,
                "updated_facts": updated,
                "skipped": skipped,
                "facts": updated,
                "facts_count": facts_count,
                "events_count": events_count,
                "remote": remote,
                "craft_trace": craft_trace,
                "idempotent": False,
            }
            request_hash = _hash(effective_prompt, assistant_text, length=64)
            connection.execute(
                """
                INSERT INTO turn_commits(
                    request_id, receipt_id, request_hash, base_revision, source_hash,
                    evidence_json, deltas_json, craft_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    receipt_id,
                    request_hash,
                    base_revision,
                    source_hash,
                    _json_dumps([delta["evidence"] for delta in deltas]),
                    _json_dumps(deltas),
                    _json_dumps(craft_trace),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE turns SET assistant_hash=?, status='committed', remote_json=?,
                    result_json=?, error='', completed_at=? WHERE request_id=?
                """,
                (
                    source_hash,
                    _json_dumps(remote),
                    _json_dumps(result),
                    now,
                    rid,
                ),
            )
            connection.commit()

        artifact_payload = {
            "schema_version": SCHEMA_VERSION,
            "request_id": rid,
            "receipt_id": receipt_id,
            "request_hash": request_hash,
            "base_revision": base_revision,
            "source_hash": source_hash,
            "created_at": now,
            "evidence": [delta["evidence"] for delta in deltas],
            "deltas": deltas,
            "craft_trace": craft_trace,
        }
        artifact_error = ""
        artifact_path = _commit_artifact_path(config, rid)
        try:
            artifact_status = _write_immutable_json(artifact_path, artifact_payload)
        except (StateRagError, OSError) as exc:
            artifact_status = "failed"
            artifact_error = str(exc)
        snapshot_error = ""
        try:
            _write_snapshot(config)
        except (StateRagError, OSError, sqlite3.Error) as exc:
            snapshot_error = str(exc)
        if snapshot_error:
            result["status"] = "degraded"
            result["snapshot"] = {"status": "failed", "reason": snapshot_error}
        else:
            result["snapshot"] = {
                "status": "ok",
                "path": str(config.snapshot_path.resolve()),
            }
        result["commit_artifact"] = {
            "status": artifact_status,
            "path": str(artifact_path),
        }
        if artifact_error:
            result["status"] = "degraded"
            result["commit_artifact"]["reason"] = artifact_error
        try:
            with _open_database(config) as connection:
                connection.execute(
                    "UPDATE turns SET result_json=? WHERE request_id=?",
                    (_json_dumps(result), rid),
                )
                connection.commit()
        except sqlite3.Error:
            result["status"] = "degraded"
            result["result_cache"] = {"status": "failed"}
        return result
    except (StateRagError, PlotRagError, OSError, sqlite3.Error, ValueError) as exc:
        return _failed_result(
            request_id=provisional_rid,
            receipt_id=provisional_receipt,
            reason=str(exc),
            commit=True,
        )


def dump_state(
    project_root: Path | str,
    subject: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """Dump current facts and immutable events, optionally filtered."""

    try:
        config = _load_runtime_config(project_root)
        if category is not None and category not in config.categories:
            raise StateRagError(f"unsupported or disabled category: {category!r}")
        clauses: list[str] = []
        params: list[Any] = []
        if subject is not None:
            clauses.append("subject = ?")
            params.append(str(subject))
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        remote = _default_remote_status(config)
        if not config.db_path.is_file():
            return {
                "status": "degraded",
                "reason": "database_not_created",
                "request_id": "",
                "receipt_id": "",
                "receipt": "",
                "subject": subject,
                "category": category,
                "facts": [],
                "events": [],
                "facts_count": 0,
                "events_count": 0,
                "total_facts_count": 0,
                "total_events_count": 0,
                "storage": {
                    "status": "not_created",
                    "path": str(config.db_path.resolve()),
                },
                "remote": remote,
            }
        with _open_diagnostic_database(config) as connection:
            facts = [
                _public_fact(row)
                for row in connection.execute(
                    "SELECT * FROM current_facts" + where + " ORDER BY category, subject, field",
                    tuple(params),
                )
            ]
            events = [
                _public_event(row)
                for row in connection.execute(
                    "SELECT * FROM state_events" + where + " ORDER BY created_at, event_id",
                    tuple(params),
                )
            ]
            total_facts, total_events = _counts(connection)
        status = "degraded" if remote["status"] == "degraded" else "ok"
        return {
            "status": status,
            "request_id": "",
            "receipt_id": "",
            "receipt": "",
            "subject": subject,
            "category": category,
            "facts": facts,
            "events": events,
            "facts_count": len(facts),
            "events_count": len(events),
            "total_facts_count": total_facts,
            "total_events_count": total_events,
            "storage": {
                "status": "ok",
                "path": str(config.db_path.resolve()),
                "read_only_snapshot": True,
            },
            "remote": remote,
        }
    except (StateRagError, PlotRagError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        return _failed_result(reason=str(exc))


def doctor(project_root: Path | str) -> dict[str, Any]:
    """Check state-RAG storage and redacted remote configuration readiness."""

    checks: list[dict[str, Any]] = []
    try:
        config = _load_runtime_config(project_root)
        loaded_config = load_config(config.root)
        checks.append({"name": "config", "status": "ok", "version": config.version})
        catalog = _load_craft_catalog()
        checks.append(
            {
                "name": "craft_catalog",
                "status": "ok" if config.craft.enabled else "disabled",
                "path": str(CRAFT_CATALOG_PATH),
                "version": catalog["version"],
                "methods_count": len(catalog["methods"]),
                "source_count": len(catalog["derived_from"]),
                "auto_retrieve": config.craft.auto_retrieve,
                "use_embedding": config.craft.use_embedding,
                "use_rerank": config.craft.use_rerank,
            }
        )
        facts_count = 0
        events_count = 0
        turns_count = 0
        database_created = config.db_path.is_file()
        db_ok = False
        continuity_ok = config.version < 3
        continuity_schema_value: int | None = None
        continuity_missing: list[str] = []
        if database_created:
            with _open_diagnostic_database(config) as connection:
                integrity_row = connection.execute("PRAGMA quick_check").fetchone()
                integrity = str(integrity_row[0] if integrity_row is not None else "unknown")
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                required = {
                    "state_meta",
                    "turns",
                    "turn_commits",
                    "state_events",
                    "current_facts",
                    "fact_vectors",
                }
                missing = sorted(required - tables)
                stored_schema = (
                    connection.execute(
                        "SELECT value FROM state_meta WHERE key='schema_version'"
                    ).fetchone()
                    if "state_meta" in tables
                    else None
                )
                if {"current_facts", "state_events"}.issubset(tables):
                    facts_count, events_count = _counts(connection)
                if "turns" in tables:
                    turns_count = int(
                        connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
                    )
                if config.version >= 3:
                    continuity_required = {
                        "entities",
                        "entity_aliases",
                        "proposals",
                        "approval_grants",
                        "canon_commits",
                        "continuity_events",
                        "canon_facts",
                        "timeless_facts",
                        "planned_facts",
                        "branch_facts",
                        "accepted_source_manifest",
                        "materialization_runs",
                        "projection_runs",
                    } | CONTINUITY_POWER_TABLES
                    continuity_missing = sorted(
                        continuity_required - tables
                    )
                    continuity_schema = (
                        connection.execute(
                            "SELECT value FROM state_meta "
                            "WHERE key='continuity_schema_version'"
                        ).fetchone()
                        if "state_meta" in tables
                        else None
                    )
                    try:
                        continuity_schema_value = (
                            None
                            if continuity_schema is None
                            else int(continuity_schema[0])
                        )
                    except (TypeError, ValueError):
                        continuity_schema_value = None
                    continuity_ok = (
                        integrity == "ok"
                        and not continuity_missing
                        and continuity_schema_value
                        == CONTINUITY_STATE_SCHEMA_VERSION
                    )
            try:
                stored_schema_value = (
                    None if stored_schema is None else int(stored_schema[0])
                )
            except (TypeError, ValueError):
                stored_schema_value = None
            db_ok = (
                integrity == "ok"
                and not missing
                and stored_schema_value == SCHEMA_VERSION
            )
            checks.append(
                {
                    "name": "database",
                    "status": "ok" if db_ok else "failed",
                    "path": str(config.db_path.resolve()),
                    "integrity": integrity,
                    "schema_version": stored_schema_value,
                    "missing_tables": missing,
                    "read_only_snapshot": True,
                }
            )
            if config.version >= 3:
                checks.append(
                    {
                        "name": "continuity_lifecycle",
                        "status": "ok" if continuity_ok else "failed",
                        "schema_version": continuity_schema_value,
                        "missing_tables": continuity_missing,
                        "strict": bool(
                            (loaded_config.get("lifecycle") or {}).get(
                                "strict", False
                            )
                        ),
                        "read_only_snapshot": True,
                    }
                )
        else:
            checks.append(
                {
                    "name": "database",
                    "status": "not_created",
                    "path": str(config.db_path.resolve()),
                    "integrity": None,
                    "schema_version": None,
                    "missing_tables": [],
                    "read_only_snapshot": True,
                }
            )
            if config.version >= 3:
                checks.append(
                    {
                        "name": "continuity_lifecycle",
                        "status": "not_created",
                        "schema_version": None,
                        "missing_tables": [],
                        "strict": bool(
                            (loaded_config.get("lifecycle") or {}).get(
                                "strict", False
                            )
                        ),
                        "read_only_snapshot": True,
                    }
                )
        snapshot = config.snapshot_path.resolve()
        checks.append(
            {
                "name": "snapshot",
                "status": "ok" if snapshot.is_file() else "not_created",
                "path": str(snapshot),
                "derived": True,
            }
        )
        initialization_path = Path(
            loaded_config["initialization"]["database_path"]
        ).resolve()
        checks.append(
            {
                "name": "initialization",
                "status": (
                    "ready" if initialization_path.is_file() else "not_created"
                ),
                "path": str(initialization_path),
                "schema_version": loaded_config["initialization"][
                    "schema_version"
                ],
                "proposal_only_before_approval": loaded_config[
                    "initialization"
                ]["proposal_only"],
            }
        )
        checks.append(
            {
                "name": "longform",
                "status": (
                    "ready"
                    if (
                        config.root
                        / ".plot-rag"
                        / "authority.v1.sqlite3"
                    ).is_file()
                    else "not_created"
                ),
                "authority_index_path": str(
                    (
                        config.root
                        / ".plot-rag"
                        / "authority.v1.sqlite3"
                    ).resolve()
                ),
                "memory_path": str(
                    (
                        config.root
                        / ".plot-rag"
                        / "longform.v1.sqlite3"
                    ).resolve()
                ),
                "projection_log_path": str(
                    (
                        config.root
                        / ".plot-rag"
                        / "projection-runs.v1.sqlite3"
                    ).resolve()
                ),
            }
        )
        remote = _default_remote_status(config)
        checks.append(
            {
                "name": "remote",
                "status": remote["status"],
                "services": remote,
            }
        )
        if not database_created:
            status = "degraded"
        elif not db_ok or not continuity_ok:
            status = "failed"
        elif remote["status"] in {"degraded", "disabled"} or not snapshot.is_file():
            status = "degraded"
        else:
            status = "ok"
        return {
            "status": status,
            "request_id": "",
            "receipt_id": "",
            "receipt": "",
            "project_root": str(config.root),
            "schema_version": SCHEMA_VERSION,
            "continuity_schema_version": continuity_schema_value,
            "checks": checks,
            "remote": remote,
            "craft": {
                "enabled": config.craft.enabled,
                "auto_retrieve": config.craft.auto_retrieve,
                "catalog_version": catalog["version"],
                "methods_count": len(catalog["methods"]),
            },
            "facts": [],
            "facts_count": facts_count,
            "events_count": events_count,
            "turns_count": turns_count,
        }
    except (StateRagError, PlotRagError, OSError, sqlite3.Error, ValueError) as exc:
        checks.append({"name": "startup", "status": "failed", "reason": str(exc)})
        result = _failed_result(reason=str(exc))
        result["checks"] = checks
        return result
