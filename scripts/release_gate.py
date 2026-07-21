#!/usr/bin/env python3
"""Portable release gates for the plot-rag-gate Codex plugin.

The repository intentionally has no third-party runtime dependencies.  This
module therefore validates the plugin, scans tracked source/history for
high-confidence secrets, builds a deterministic package round-trip, and
compares an installed cache with the authoritative source using only the
Python standard library and Git.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence


SEMVER_RE = re.compile(
    r"^(?P<base>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
CACHEBUSTER_VERSION_RE = re.compile(
    r"^(?P<base>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"
    r"\+codex\.(?P<token>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)
RELEASE_CACHEBUSTER_RE = re.compile(r"^\d{14}$")
PLUGIN_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CHANGELOG_VERSION_RE = re.compile(r"^##\s+(\d+\.\d+\.\d+)\b", re.MULTILINE)
PLUGIN_ROOT_REFERENCE_RE = re.compile(
    r"\$\{CLAUDE_PLUGIN_ROOT\}[\\/](?P<path>[^\"'\s]+)"
)
NOISE_PARTS = frozenset(
    {
        ".git",
        ".plot-rag",
        ".plot-rag-benchmark",
        ".plot-rag-init",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "dist",
        "build",
        "htmlcov",
    }
)
NOISE_NAMES = frozenset(
    {
        ".plot-rag-current-project",
    }
)
NOISE_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
)
CI_REQUIRED_JOB_COMMANDS = {
    "test": (
        "python -B -X utf8 -m unittest discover -s tests -v",
        "python -B -X utf8 scripts/release_gate.py smoke --root .",
    ),
    "release-gates": (
        "python -B -X utf8 scripts/release_gate.py validate --root .",
        "python -B -X utf8 scripts/release_gate.py secrets --root . --history",
        "python -B -X utf8 scripts/release_gate.py roundtrip --root .",
    ),
}
CI_REQUIRED_TRIGGERS = frozenset(
    {
        "pull_request",
        "push",
        "workflow_dispatch",
    }
)
EXPECTED_MCP_SERVER_NAME = "plot-rag-state"
EXPECTED_MCP_CWD = "."
EXPECTED_MCP_ARGUMENTS = (
    "-B",
    "-X",
    "utf8",
    "./scripts/plot_rag_mcp.py",
)
EXPECTED_HOOK_ARGUMENTS = {
    "SessionStart": (
        "-B",
        "-X",
        "utf8",
        "${CLAUDE_PLUGIN_ROOT}/hooks/plot_progression_gate.py",
        "--session-start",
    ),
    "UserPromptSubmit": (
        "-B",
        "-X",
        "utf8",
        "${CLAUDE_PLUGIN_ROOT}/hooks/plot_progression_gate.py",
    ),
    "Stop": (
        "-B",
        "-X",
        "utf8",
        "${CLAUDE_PLUGIN_ROOT}/hooks/plot_progression_gate.py",
        "--stop",
    ),
}
EXPECTED_HOOK_TIMEOUTS = {
    "SessionStart": 5,
    "UserPromptSubmit": 75,
    "Stop": 45,
}
WINDOWS_RESERVED_PATH_NAMES = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)
WINDOWS_INVALID_PATH_CHARACTERS = frozenset('<>:"|?*')
PATH_ARGUMENT_SUFFIXES = frozenset(
    {
        ".bat",
        ".cfg",
        ".cmd",
        ".conf",
        ".exe",
        ".ini",
        ".json",
        ".jsonl",
        ".md",
        ".ps1",
        ".py",
        ".sh",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
MARKETPLACE_INSTALLATION_POLICIES = frozenset(
    {"NOT_AVAILABLE", "AVAILABLE", "INSTALLED_BY_DEFAULT"}
)
MARKETPLACE_AUTHENTICATION_POLICIES = frozenset({"ON_INSTALL", "ON_USE"})
SHELL_UNSAFE_PATH_RE = re.compile(
    r"(?:^|[\s\"'=])(?:"
    r"[A-Za-z]:[\\/]|\\\\"
    r"|~(?:[A-Za-z0-9._-]+)?(?=$|[\\/\s\"'])"
    r"|\.\.[\\/])"
    r"|[\\/]\.\.(?=[\\/\s\"']|$)"
)
SHELL_DYNAMIC_PATH_RE = re.compile(
    r"[$`%!*?\[\];|&<>(){}#]"
    r"|(?:^|[\s\"'=])~"
)
EXPLICIT_SECRET_FIXTURE_VALUES = frozenset(
    {
        "YOUR_API_KEY_PLACEHOLDER",
        "YOUR_SILICONFLOW_API_KEY",
    }
)
EXPLICIT_SECRET_FIXTURE_PATHS = frozenset({".env.example"})
PAYLOAD_ALLOWED_ROOT_FILES = frozenset(
    {
        ".app.json",
        ".editorconfig",
        ".gitattributes",
        ".gitignore",
        ".mcp.json",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "LICENSE.md",
        "NOTICE",
        "POWER_SYSTEM_ADAPTATION_PLAN.md",
        "POWER_SYSTEM_MIGRATION.md",
        "README.md",
        "SECURITY.md",
        "V1_5_MIGRATION.md",
        "V1_6_MIGRATION.md",
        "WEBNOVEL_INITIALIZATION_FRAMEWORK.md",
    }
)
PAYLOAD_ALLOWED_BENCHMARK_FILES = frozenset(
    {
        "benchmarks/README.md",
        "benchmarks/__init__.py",
        "benchmarks/advantage_performance.py",
        "benchmarks/advantage_profile_fixtures.py",
        "benchmarks/fixtures/advantage_profile_matrix.v1.json",
        "benchmarks/fixtures/advantage_prompts.v1.jsonl",
        "benchmarks/fixtures/chapters_500.v1.jsonl",
        "benchmarks/fixtures/event_experience_annotations.v1.jsonl",
        "benchmarks/fixtures/item_function_annotations.v1.jsonl",
        "benchmarks/fixtures/longform_annotations.v1.jsonl",
        "benchmarks/fixtures/power_system_annotations.v1.jsonl",
        "benchmarks/fixtures/remote_responses.v1.json",
        "benchmarks/fixtures/v15_performance_manifest.v1.json",
        "benchmarks/fixtures/v15_generic_live_prompts.v1.json",
        "benchmarks/generate_fixtures.py",
        "benchmarks/generate_power_fixtures.py",
        "benchmarks/generate_v15_performance_fixtures.py",
        "benchmarks/run_longform_benchmark.py",
        "benchmarks/run_v15_live_e2e.py",
        "benchmarks/run_v15_performance_benchmark.py",
        "benchmarks/v15_live_e2e.py",
        "benchmarks/v15_performance.py",
    }
)
PAYLOAD_ALLOWED_PREFIXES = (
    ".codex-plugin/",
    ".github/",
    "agents/",
    "assets/",
    "commands/",
    "docs/",
    "hooks/",
    "knowledge/",
    "schemas/",
    "scripts/",
    "skills/",
    "templates/",
    "tests/",
)
LF_TEXT_SUFFIXES = frozenset(
    {".json", ".jsonl", ".md", ".py", ".yaml", ".yml"}
)
LF_TEXT_NAMES = frozenset(
    {
        ".editorconfig",
        ".gitattributes",
        ".gitignore",
        ".mcp.json",
    }
)
V15_CONFIG_DEFAULTS: Mapping[tuple[str, ...], Any] = {
    ("extraction_protocol", "authoritative_protocol"): "json_object",
    ("extraction_protocol", "tool_schema_shadow"): False,
    ("extraction_protocol", "tool_name"): "submit_plot_rag_deltas",
    ("performance", "prepare_v2", "enabled"): False,
    ("performance", "prepare_v2", "shadow"): True,
    ("performance", "prepare_v2", "single_read_snapshot"): True,
    ("performance", "prepare_v2", "exact_state_short_circuit"): True,
    ("performance", "prepare_v2", "batch_embedding"): True,
    ("performance", "prepare_v2", "batch_failure_fallback_single"): True,
    ("performance", "prepare_v2", "rerank_max_concurrency"): 4,
    ("performance", "prepare_v2", "remote_total_concurrency"): 6,
    ("performance", "prepare_v2", "singleflight"): True,
    ("performance", "prepare_v2", "persistent_exact_cache"): True,
    ("performance", "prepare_v2", "http_keep_alive"): True,
    ("performance", "extraction", "mode"): "sync",
    ("performance", "extraction", "async_shadow"): True,
    ("performance", "extraction", "next_plot_turn_barrier"): True,
    (
        "performance",
        "extraction",
        "barrier_requires_proposal_resolution",
    ): True,
    (
        "performance",
        "extraction",
        "deterministic_repairs",
    ): ["single_action_event_type_echo"],
    ("event_experience", "enabled"): True,
    ("event_experience", "required_before_event_design"): True,
    ("event_experience", "event_seed_required"): True,
    ("event_experience", "receipt_hash_binding"): True,
    ("event_experience", "derive_from_intent"): True,
    ("event_experience", "grill_on_structural_ambiguity"): True,
    ("event_experience", "one_question_per_turn"): True,
    ("event_experience", "max_questions_per_chain"): 1,
    ("event_experience", "repeat_same_question_limit"): 1,
    ("event_experience", "session_ttl_seconds"): 21600,
    ("event_experience", "visible_in_story_artifacts"): False,
    ("items", "schema_version"): "plot-rag-item/v1",
    ("items", "delta_version"): "plot-rag-delta/v4",
    ("items", "strict_runtime_validation"): False,
    ("items", "power_binding_bridge"): True,
    ("items", "readable_projection"): True,
    ("advantage", "enabled"): False,
    ("advantage", "shadow"): True,
    ("advantage", "schema_version"): "plot-rag-advantage/v1",
    ("advantage", "strict_runtime_validation"): False,
    ("advantage", "readable_projection"): True,
    ("advantage", "mandatory_context"): True,
}
V15_ITEM_EVENT_TYPES = frozenset(
    {
        "item_spec",
        "item_instance",
        "item_custody",
        "item_runtime",
        "item_use",
        "item_observation",
        "item_correction",
    }
)
V15_LEGACY_EVENT_TYPES = frozenset({"inventory"})
V15_REQUIRED_SCHEMA_IDS: Mapping[str, str] = {
    "schemas/plot-rag-delta/v4.schema.json": (
        "https://plot-rag-gate.local/schemas/plot-rag-delta/v4.schema.json"
    ),
    "schemas/plot-rag-delta/v4/common.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-delta/v4/common.schema.json"
    ),
    "schemas/plot-rag-delta/v4/envelope.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-delta/v4/envelope.schema.json"
    ),
    "schemas/plot-rag-delta/v4/item-candidate.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-delta/v4/item-candidate.schema.json"
    ),
    "schemas/plot-rag-delta/v4/legacy-v3-delta.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-delta/v4/legacy-v3-delta.schema.json"
    ),
    "schemas/plot-rag-item/v1.schema.json": (
        "https://plot-rag-gate.local/schemas/plot-rag-item/v1.schema.json"
    ),
    "schemas/plot-rag-item.v1.json": (
        "https://plot-rag-gate.local/schemas/plot-rag-item.v1.json"
    ),
    "schemas/plot-rag-advantage/v1.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-advantage/v1.schema.json"
    ),
    "schemas/plot-rag-advantage.v1.json": (
        "https://plot-rag-gate.local/schemas/plot-rag-advantage.v1.json"
    ),
    "schemas/plot-rag-event-experience/v1/common.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-event-experience/v1/common.schema.json"
    ),
    (
        "schemas/plot-rag-event-experience/v1/"
        "event-experience-arc.schema.json"
    ): (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-event-experience/v1/event-experience-arc.schema.json"
    ),
    (
        "schemas/plot-rag-event-experience/v1/"
        "event-experience-contract.schema.json"
    ): (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-event-experience/v1/"
        "event-experience-contract.schema.json"
    ),
    "schemas/plot-rag-event-experience/v1/event-seed.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-event-experience/v1/event-seed.schema.json"
    ),
    "schemas/plot-rag-event-experience/v1/experience-review.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-event-experience/v1/experience-review.schema.json"
    ),
    "schemas/plot-rag-event-experience/v1/manifest.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-event-experience/v1/manifest.schema.json"
    ),
    "schemas/plot-rag-event-experience/v1/question.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-event-experience/v1/question.schema.json"
    ),
    "schemas/plot-rag-extraction/v1/barrier-status.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-extraction/v1/barrier-status.schema.json"
    ),
    "schemas/plot-rag-extraction/v1/common.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-extraction/v1/common.schema.json"
    ),
    "schemas/plot-rag-extraction/v1/extraction-job.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-extraction/v1/extraction-job.schema.json"
    ),
    "schemas/plot-rag-extraction/v1/worker-result.schema.json": (
        "https://plot-rag-gate.local/schemas/"
        "plot-rag-extraction/v1/worker-result.schema.json"
    ),
}
V15_REQUIRED_SCHEMA_MARKERS: Mapping[
    str,
    tuple[tuple[tuple[str, ...], Any], ...],
] = {
    "schemas/plot-rag-delta/v4.schema.json": (
        (
            ("$ref",),
            "v4/envelope.schema.json",
        ),
    ),
    "schemas/plot-rag-delta/v4/envelope.schema.json": (
        (
            ("properties", "schema_version", "const"),
            "plot-rag-delta/v4",
        ),
    ),
    "schemas/plot-rag-item/v1.schema.json": (
        (
            ("$ref",),
            "../plot-rag-item.v1.json",
        ),
    ),
    "schemas/plot-rag-item.v1.json": (
        (
            ("properties", "schema_version", "const"),
            "plot-rag-item/v1",
        ),
    ),
    "schemas/plot-rag-advantage/v1.schema.json": (
        (
            ("$ref",),
            "../plot-rag-advantage.v1.json",
        ),
    ),
    "schemas/plot-rag-advantage.v1.json": (
        (
            ("properties", "schema_version", "const"),
            "plot-rag-advantage/v1",
        ),
    ),
    (
        "schemas/plot-rag-event-experience/v1/"
        "event-experience-contract.schema.json"
    ): (
        (
            ("properties", "schema_version", "const"),
            "plot-rag-event-experience/v1",
        ),
    ),
    "schemas/plot-rag-event-experience/v1/manifest.schema.json": (
        (
            (
                "$defs",
                "candidateManifest",
                "properties",
                "schema_version",
                "const",
            ),
            "plot-rag-event-experience/v1",
        ),
        (
            (
                "$defs",
                "lockedManifest",
                "properties",
                "schema_version",
                "const",
            ),
            "plot-rag-event-experience/v1",
        ),
    ),
}
V15_REQUIRED_ADAPTER_PATHS = (
    "scripts/state_rag.py",
    "scripts/continuity/validators.py",
    "scripts/continuity/items.py",
)
V15_REQUIRED_PAYLOAD_PATHS = tuple(V15_REQUIRED_SCHEMA_IDS) + (
    V15_REQUIRED_ADAPTER_PATHS
)
V15_STATE_RAG_ADAPTER_EXPORTS = frozenset(
    {
        "normalize_item_extraction_candidate",
        "normalize_advantage_extraction_candidate",
        "validate_delta_v4_envelope",
        "split_delta_v4_results",
        "split_delta_v4_results_by_family",
        "adapt_item_extraction_candidate",
        "adapt_item_extraction_candidates",
        "adapt_advantage_extraction_candidate",
        "adapt_advantage_extraction_candidates",
    }
)
V15_ADAPTER_FUNCTION_CONTRACTS: Mapping[
    tuple[str, str],
    tuple[frozenset[str], frozenset[str]],
] = {
    (
        "scripts/state_rag.py",
        "validate_delta_v4_envelope",
    ): (
        frozenset(
            {
                "_validate_v3_deltas",
                "normalize_item_extraction_candidate",
                "normalize_advantage_extraction_candidate",
            }
        ),
        frozenset(
            {
                "DELTA_V3_SCHEMA",
                "DELTA_V4_SCHEMA",
                "ITEM_DELTA_EVENT_TYPES",
                "ADVANTAGE_DELTA_EVENT_TYPES",
                "normalize_item_extraction_candidate",
                "normalize_advantage_extraction_candidate",
            }
        ),
    ),
    (
        "scripts/state_rag.py",
        "split_delta_v4_results",
    ): (
        frozenset(),
        frozenset(
            {
                "DELTA_V3_SCHEMA",
                "DELTA_V4_SCHEMA",
                "ITEM_DELTA_EVENT_TYPES",
                "ADVANTAGE_DELTA_EVENT_TYPES",
            }
        ),
    ),
    (
        "scripts/state_rag.py",
        "split_delta_v4_results_by_family",
    ): (
        frozenset(),
        frozenset(
            {
                "DELTA_V3_SCHEMA",
                "DELTA_V4_SCHEMA",
                "ITEM_DELTA_EVENT_TYPES",
                "ADVANTAGE_DELTA_EVENT_TYPES",
            }
        ),
    ),
    (
        "scripts/state_rag.py",
        "adapt_item_extraction_candidate",
    ): (
        frozenset({"normalize_item_extraction_candidate"}),
        frozenset({"DELTA_V4_SCHEMA"}),
    ),
    (
        "scripts/state_rag.py",
        "adapt_item_extraction_candidates",
    ): (
        frozenset({"adapt_item_extraction_candidate"}),
        frozenset(),
    ),
    (
        "scripts/state_rag.py",
        "adapt_advantage_extraction_candidate",
    ): (
        frozenset({"normalize_advantage_extraction_candidate"}),
        frozenset({"DELTA_V4_SCHEMA", "ADVANTAGE_EVENT_SCHEMA"}),
    ),
    (
        "scripts/state_rag.py",
        "adapt_advantage_extraction_candidates",
    ): (
        frozenset({"adapt_advantage_extraction_candidate"}),
        frozenset(),
    ),
    (
        "scripts/state_rag.py",
        "_validate_deltas",
    ): (
        frozenset({"validate_delta_v4_envelope"}),
        frozenset({"DELTA_V3_SCHEMA", "DELTA_V4_SCHEMA"}),
    ),
    (
        "scripts/state_rag.py",
        "commit_turn",
    ): (
        frozenset({"split_delta_v4_results"}),
        frozenset(),
    ),
    (
        "scripts/continuity/validators.py",
        "normalize_event",
    ): (
        frozenset(
            {
                "_normalize_item_envelope_fields",
                "_normalize_item_spec_event",
                "_normalize_item_instance_event",
                "_normalize_item_custody_event",
                "_normalize_item_runtime_event",
                "_normalize_item_use_event",
                "_normalize_item_observation_event",
            }
        ),
        frozenset({"ITEM_EVENT_TYPES"}),
    ),
    (
        "scripts/continuity/validators.py",
        "_normalize_item_envelope_fields",
    ): (
        frozenset(),
        frozenset({"ITEM_DELTA_SCHEMA_VERSION"}),
    ),
    (
        "scripts/continuity/items.py",
        "validate_item_event_sequence",
    ): (
        frozenset(),
        frozenset({"ITEM_DELTA_SCHEMA_VERSION", "ITEM_EVENT_TYPES"}),
    ),
}

ADVANTAGE_V1_REQUIRED_PROFILES = frozenset(
    {
        "appraisal_copy",
        "bloodline_constitution",
        "companion_mentor",
        "contract_summon",
        "foreknowledge",
        "growth_relic",
        "inheritance",
        "pocket_domain",
        "resource_transformer",
        "reward_market",
        "sign_in_lottery",
        "simulator_branch",
        "social_currency",
        "system_panel",
        "task_reward",
        "time_causality",
    }
)
ADVANTAGE_V1_REQUIRED_CLI_PARSERS = frozenset(
    {
        "advantage",
        "definition",
        "anchors",
        "anchor",
        "runtime",
        "modules",
        "ledger",
        "knowledge",
        "progression",
        "exposure",
        "special-item",
        "context",
        "inventory",
        "special-item-context",
    }
)
ADVANTAGE_V1_REQUIRED_QUERY_HELPERS = frozenset(
    {
        "query_advantage_definition",
        "query_advantage_anchors",
        "query_advantage_runtime",
        "query_advantage_modules",
        "query_advantage_ledger",
        "query_advantage_knowledge",
        "query_advantage_progression",
        "query_advantage_exposure",
        "query_advantage_context",
    }
)
ADVANTAGE_V1_REQUIRED_MCP_TOOLS = frozenset(
    {
        "query_advantage_definition",
        "query_advantage_anchors",
        "query_advantage_runtime",
        "query_advantage_modules",
        "query_advantage_ledger",
        "query_advantage_knowledge",
        "query_advantage_progression",
        "query_advantage_exposure",
        "query_special_item_context",
    }
)
ADVANTAGE_V1_VISIBILITY_MODES = ("generation", "inspection", "raw")
ADVANTAGE_V1_VISIBILITY_DEFAULT = "generation"
ADVANTAGE_V1_REQUIRED_SOURCE_PATHS = (
    "templates/advantage_profiles.v1.json",
    "scripts/advantage_profiles.py",
    "scripts/continuity/advantages.py",
    "scripts/plot_init/advantages.py",
    "scripts/plot_state.py",
    "scripts/plot_rag_mcp.py",
    "scripts/plot_rag.py",
    "scripts/v1_runtime.py",
    "hooks/plot_progression_gate.py",
    "tests/test_advantage_hook_context.py",
)
SOURCE_MANIFEST_REQUIRED_SOURCE_PATHS = (
    "scripts/continuity/source_manifest.py",
    "scripts/continuity/service.py",
    "scripts/continuity/replay.py",
    "scripts/v1_runtime.py",
    "scripts/plot_state.py",
    "scripts/plot_rag_mcp.py",
    "tests/test_source_manifest_lifecycle.py",
)
SOURCE_MANIFEST_REQUIRED_RUNTIME_HELPERS = frozenset(
    {
        "source_manifest_status",
        "preview_source_manifest_change",
        "propose_source_manifest_change",
    }
)
SOURCE_MANIFEST_REQUIRED_MCP_TOOLS = {
    "get_source_manifest_status": True,
    "preview_source_manifest_change": True,
    "propose_source_manifest_change": False,
}
POWER_SPEC_REQUIRED_SOURCE_PATHS = (
    "scripts/continuity/power_spec.py",
    "scripts/continuity/service.py",
    "scripts/v1_runtime.py",
    "scripts/plot_state.py",
    "scripts/plot_rag_mcp.py",
    "tests/test_power_spec_import.py",
    "tests/test_power_spec_lifecycle.py",
    "tests/test_cli.py",
    "tests/test_mcp.py",
)
POWER_SPEC_REQUIRED_CORE_FUNCTIONS = frozenset(
    {
        "build_power_spec_lifecycle_package",
        "compile_power_spec_change",
        "preview_power_spec_import",
        "validate_power_spec_import",
        "validate_power_spec_lifecycle_package",
    }
)
POWER_SPEC_REQUIRED_RUNTIME_HELPERS = frozenset(
    {
        "validate_power_spec_change",
        "preview_power_spec_change",
        "propose_power_spec_change",
    }
)
POWER_SPEC_REQUIRED_SERVICE_METHODS = frozenset(
    {
        "preview_power_spec_change",
        "propose_power_spec_change",
    }
)
POWER_SPEC_REQUIRED_CLI_LITERALS = frozenset(
    {
        "power-spec",
        "validate",
        "preview",
        "propose",
        "--spec",
        "--spec-json",
        "--expected-canon-revision",
        "--idempotency-key",
    }
)
POWER_SPEC_REQUIRED_MCP_TOOLS: Mapping[
    str,
    tuple[bool, tuple[str, ...]],
] = {
    "validate_power_spec_change": (
        True,
        ("power_spec",),
    ),
    "preview_power_spec_change": (
        True,
        (
            "project_root",
            "power_spec",
            "expected_canon_revision",
        ),
    ),
    "propose_power_spec_change": (
        False,
        (
            "project_root",
            "power_spec",
            "expected_canon_revision",
            "idempotency_key",
        ),
    ),
}
POWER_SPEC_COLLECTIONS_CONTRACT = (
    (
        "power_systems",
        "power_system_id",
        "power_system",
        "power_system",
    ),
    (
        "progression_tracks",
        "track_id",
        "progression_track",
        "progression_track",
    ),
    ("rank_nodes", "rank_node_id", "rank_node", "rank_node"),
    ("rank_edges", "rank_edge_id", "rank_edge", "rank_edge"),
    (
        "ability_definitions",
        "ability_id",
        "ability_definition",
        "ability",
    ),
    (
        "resource_definitions",
        "resource_id",
        "resource_definition",
        "resource_pool",
    ),
    (
        "status_definitions",
        "status_id",
        "status_definition",
        "status_effect",
    ),
    (
        "qualification_definitions",
        "qualification_id",
        "qualification_definition",
        "qualification",
    ),
    (
        "counter_rules",
        "counter_rule_id",
        "counter_rule",
        "counter_rule",
    ),
    ("bridge_rules", "bridge_rule_id", "bridge_rule", "bridge_rule"),
    (
        "conversion_rules",
        "conversion_rule_id",
        "conversion_rule",
        "conversion_rule",
    ),
)


@dataclass(frozen=True)
class GateIssue:
    code: str
    path: str
    message: str
    line: int | None = None

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{self.code} | {location} | {self.message}"


@dataclass(frozen=True)
class SecretFinding:
    scope: str
    path: str
    line: int
    kind: str
    masked: str

    def render(self) -> str:
        return (
            f"{self.kind} | {self.scope} | {self.path}:{self.line} | "
            f"{self.masked}"
        )


@dataclass(frozen=True)
class TreeComparison:
    missing: tuple[str, ...]
    mismatched: tuple[str, ...]
    unexpected: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (self.missing or self.mismatched or self.unexpected)


@dataclass(frozen=True)
class TrackedPath:
    path: str
    mode: str | None
    stage: int
    object_id: str | None = None


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _reject_duplicate_json_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError(
                f"duplicate object key {key!r}",
                key,
                0,
            )
        result[key] = value
    return result


def _load_json(path: Path) -> Any:
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_json_pairs,
    )


def _relative(root: Path, path: Path) -> str:
    return path.resolve(strict=False).relative_to(
        root.resolve(strict=False)
    ).as_posix()


def _semantic_base(version: str) -> str | None:
    matched = SEMVER_RE.fullmatch(str(version or "").strip())
    return matched.group("base") if matched else None


def _cachebuster_base(version: str) -> str | None:
    matched = CACHEBUSTER_VERSION_RE.fullmatch(str(version or "").strip())
    return matched.group("base") if matched else None


def _cachebuster_token(version: str) -> str | None:
    matched = CACHEBUSTER_VERSION_RE.fullmatch(str(version or "").strip())
    return matched.group("token") if matched else None


def _is_release_cachebuster(token: str | None) -> bool:
    if not RELEASE_CACHEBUSTER_RE.fullmatch(str(token or "")):
        return False
    try:
        datetime.strptime(str(token), "%Y%m%d%H%M%S")
    except ValueError:
        return False
    return True


def _python_string_constant(path: Path, name: str) -> str | None:
    literal = _python_literal_constant(path, name)
    return str(literal) if literal is not None else None


def _bound_target_names(target: ast.expr) -> tuple[str, ...]:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, (ast.Tuple, ast.List)):
        return tuple(
            name
            for element in target.elts
            for name in _bound_target_names(element)
        )
    if isinstance(target, ast.Starred):
        return _bound_target_names(target.value)
    return ()


def _top_level_bindings(
    tree: ast.Module,
    name: str,
) -> tuple[tuple[ast.AST, ast.expr | None], ...]:
    bindings: list[tuple[ast.AST, ast.expr | None]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                bindings.append((node, None))
            continue
        if isinstance(node, ast.Assign):
            if any(
                name in _bound_target_names(target)
                for target in node.targets
            ):
                bindings.append((node, node.value))
            continue
        if isinstance(node, ast.AnnAssign):
            if name in _bound_target_names(node.target):
                bindings.append((node, node.value))
            continue
        if isinstance(node, ast.AugAssign):
            if name in _bound_target_names(node.target):
                bindings.append((node, node.value))
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                if bound == name:
                    bindings.append((node, None))
            continue
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    bindings.append((node, None))
    return tuple(bindings)


def _python_literal_constant(path: Path, name: str) -> Any | None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bindings = _top_level_bindings(tree, name)
    if not bindings:
        return None
    if len(bindings) != 1:
        raise ValueError(
            f"{name} must have exactly one top-level binding; "
            f"found {len(bindings)}"
        )
    _node, value = bindings[0]
    if value is None:
        raise ValueError(f"{name} must be a literal top-level assignment")
    return ast.literal_eval(value)


def _mapping_path_value(
    payload: Mapping[str, Any],
    path: Sequence[str],
) -> tuple[bool, Any]:
    current: Any = payload
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _top_level_function(
    tree: ast.Module,
    name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    bindings = _top_level_bindings(tree, name)
    if len(bindings) != 1:
        return None
    node, _value = bindings[0]
    return (
        node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        else None
    )


def _function_contract_surface(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[frozenset[str], frozenset[str]]:
    class ContractVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.called_names: set[str] = set()
            self.referenced_names: set[str] = set()

        def visit_Name(self, child: ast.Name) -> None:
            if isinstance(child.ctx, ast.Load):
                self.referenced_names.add(child.id)

        def visit_Call(self, child: ast.Call) -> None:
            if isinstance(child.func, ast.Name):
                self.called_names.add(child.func.id)
            self.generic_visit(child)

        def visit_FunctionDef(self, child: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(
            self,
            child: ast.AsyncFunctionDef,
        ) -> None:
            return

        def visit_Lambda(self, child: ast.Lambda) -> None:
            return

        def visit_ClassDef(self, child: ast.ClassDef) -> None:
            return

    visitor = ContractVisitor()
    for statement in node.body:
        visitor.visit(statement)
    return (
        frozenset(visitor.called_names),
        frozenset(visitor.referenced_names),
    )


def _validate_v15_schema_surface(root: Path) -> list[GateIssue]:
    issues: list[GateIssue] = []
    schema_root = (root / "schemas").resolve(strict=False)
    documents: dict[Path, Mapping[str, Any]] = {}
    ids: dict[str, Path] = {}

    if schema_root.is_dir():
        for path in sorted(schema_root.rglob("*.json")):
            try:
                document = _load_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(document, Mapping):
                continue
            documents[path.resolve()] = document
            schema_id = document.get("$id")
            if isinstance(schema_id, str) and schema_id:
                ids.setdefault(schema_id, path.resolve())

    pending: list[Path] = []
    for relative, expected_id in V15_REQUIRED_SCHEMA_IDS.items():
        if not _is_allowed_payload_file(relative):
            issues.append(
                GateIssue(
                    "V15_PAYLOAD_SURFACE_INVALID",
                    relative,
                    "required v1.5 schema is outside the release payload allowlist",
                )
            )
        try:
            path, _ = _repository_reference(
                root,
                relative,
                expect="file",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            issues.append(
                GateIssue(
                    "V15_SCHEMA_REQUIRED_MISSING",
                    relative,
                    str(exc),
                )
            )
            continue
        resolved = path.resolve()
        try:
            document = _load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(
                GateIssue(
                    "V15_SCHEMA_INVALID",
                    relative,
                    str(exc),
                )
            )
            continue
        if not isinstance(document, Mapping):
            issues.append(
                GateIssue(
                    "V15_SCHEMA_INVALID",
                    relative,
                    "schema root must be an object",
                )
            )
            continue
        documents[resolved] = document
        if document.get("$schema") != (
            "https://json-schema.org/draft/2020-12/schema"
        ):
            issues.append(
                GateIssue(
                    "V15_SCHEMA_CONTRACT_MISMATCH",
                    relative,
                    "schema must declare JSON Schema draft 2020-12",
                )
            )
        if document.get("$id") != expected_id:
            issues.append(
                GateIssue(
                    "V15_SCHEMA_CONTRACT_MISMATCH",
                    relative,
                    f"$id must be {expected_id!r}; found {document.get('$id')!r}",
                )
            )
        for marker_path, expected in V15_REQUIRED_SCHEMA_MARKERS.get(
            relative,
            (),
        ):
            present, actual = _mapping_path_value(document, marker_path)
            if (
                not present
                or type(actual) is not type(expected)
                or actual != expected
            ):
                issues.append(
                    GateIssue(
                        "V15_SCHEMA_CONTRACT_MISMATCH",
                        relative,
                        (
                            f"{'.'.join(marker_path)} must be {expected!r}; "
                            f"found {actual!r}"
                            if present
                            else f"{'.'.join(marker_path)} is required"
                        ),
                    )
                )
        pending.append(resolved)

    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        document = documents.get(path)
        if document is None:
            try:
                loaded = _load_json(path)
            except (OSError, json.JSONDecodeError) as exc:
                relative = (
                    _relative(root, path)
                    if root.resolve() in path.parents
                    else str(path)
                )
                issues.append(
                    GateIssue("V15_SCHEMA_INVALID", relative, str(exc))
                )
                continue
            if not isinstance(loaded, Mapping):
                relative = (
                    _relative(root, path)
                    if root.resolve() in path.parents
                    else str(path)
                )
                issues.append(
                    GateIssue(
                        "V15_SCHEMA_INVALID",
                        relative,
                        "referenced schema root must be an object",
                    )
                )
                continue
            document = loaded
            documents[path] = document

        source_relative = (
            _relative(root, path)
            if root.resolve() in path.parents
            else str(path)
        )
        for location, value in _walk_json(document):
            if not isinstance(value, Mapping) or not isinstance(
                value.get("$ref"),
                str,
            ):
                continue
            reference = str(value["$ref"])
            if "\\" in reference:
                issues.append(
                    GateIssue(
                        "V15_SCHEMA_REF_INVALID",
                        source_relative,
                        (
                            f"{location} uses a non-portable backslash "
                            f"schema reference: {reference}"
                        ),
                    )
                )
                continue
            base, fragment = urllib.parse.urldefrag(reference)
            if base.startswith(("http://", "https://")):
                target = ids.get(base)
            else:
                target = (
                    (path.parent / urllib.parse.unquote(base)).resolve()
                    if base
                    else path
                )
                if (
                    target != schema_root
                    and schema_root not in target.parents
                ):
                    target = None
            if target is None or not target.is_file():
                issues.append(
                    GateIssue(
                        "V15_SCHEMA_REF_INVALID",
                        source_relative,
                        f"{location} references missing schema: {reference}",
                    )
                )
                continue
            target = target.resolve()
            target_document = documents.get(target)
            if target_document is None:
                try:
                    loaded = _load_json(target)
                except (OSError, json.JSONDecodeError) as exc:
                    issues.append(
                        GateIssue(
                            "V15_SCHEMA_REF_INVALID",
                            source_relative,
                            f"{location} references invalid schema {reference}: {exc}",
                        )
                    )
                    continue
                if not isinstance(loaded, Mapping):
                    issues.append(
                        GateIssue(
                            "V15_SCHEMA_REF_INVALID",
                            source_relative,
                            (
                                f"{location} references non-object schema: "
                                f"{reference}"
                            ),
                        )
                    )
                    continue
                target_document = loaded
                documents[target] = target_document
            pointer = (
                f"#{urllib.parse.unquote(fragment)}"
                if fragment
                else "#"
            )
            if not _json_pointer_exists(target_document, pointer):
                issues.append(
                    GateIssue(
                        "V15_SCHEMA_REF_INVALID",
                        source_relative,
                        f"{location} references missing fragment: {reference}",
                    )
                )
                continue
            pending.append(target)

    return issues


def _validate_v15_adapter_surface(root: Path) -> list[GateIssue]:
    issues: list[GateIssue] = []
    trees: dict[str, ast.Module] = {}
    for relative in V15_REQUIRED_ADAPTER_PATHS:
        if not _is_allowed_payload_file(relative):
            issues.append(
                GateIssue(
                    "V15_PAYLOAD_SURFACE_INVALID",
                    relative,
                    "required v1.5 adapter is outside the release payload allowlist",
                )
            )
        try:
            path, _ = _repository_reference(
                root,
                relative,
                expect="file",
            )
            trees[relative] = ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
            )
        except (OSError, RuntimeError, SyntaxError, ValueError) as exc:
            issues.append(
                GateIssue(
                    "V15_ADAPTER_CONTRACT_INVALID",
                    relative,
                    str(exc),
                )
            )

    state_path = root / "scripts" / "state_rag.py"
    try:
        legacy_delta = _python_string_constant(
            state_path,
            "DELTA_V3_SCHEMA",
        )
        item_delta = _python_string_constant(
            state_path,
            "DELTA_V4_SCHEMA",
        )
        exports = _python_literal_constant(state_path, "__all__")
    except (OSError, SyntaxError, ValueError) as exc:
        issues.append(
            GateIssue(
                "V15_DELTA_COMPATIBILITY_INVALID",
                "scripts/state_rag.py",
                str(exc),
            )
        )
        legacy_delta = None
        item_delta = None
        exports = None
    if legacy_delta != "plot-rag-delta/v3":
        issues.append(
            GateIssue(
                "V15_DELTA_COMPATIBILITY_MISMATCH",
                "scripts/state_rag.py",
                (
                    "DELTA_V3_SCHEMA must remain plot-rag-delta/v3; "
                    f"found {legacy_delta!r}"
                ),
            )
        )
    if item_delta != "plot-rag-delta/v4":
        issues.append(
            GateIssue(
                "V15_DELTA_COMPATIBILITY_MISMATCH",
                "scripts/state_rag.py",
                (
                    "DELTA_V4_SCHEMA must be plot-rag-delta/v4; "
                    f"found {item_delta!r}"
                ),
            )
        )
    normalized_exports = (
        frozenset(str(value) for value in exports)
        if isinstance(exports, (tuple, list, set, frozenset))
        else frozenset()
    )
    missing_exports = sorted(
        V15_STATE_RAG_ADAPTER_EXPORTS - normalized_exports
    )
    if missing_exports:
        issues.append(
            GateIssue(
                "V15_ADAPTER_CONTRACT_MISMATCH",
                "scripts/state_rag.py",
                f"public v4 adapter exports are missing: {missing_exports!r}",
            )
        )

    for relative, constant_name in (
        ("scripts/continuity/validators.py", "ITEM_DELTA_SCHEMA_VERSION"),
        ("scripts/continuity/items.py", "ITEM_DELTA_SCHEMA_VERSION"),
    ):
        try:
            value = _python_string_constant(root / relative, constant_name)
        except (OSError, SyntaxError, ValueError) as exc:
            issues.append(
                GateIssue(
                    "V15_ADAPTER_CONTRACT_INVALID",
                    relative,
                    str(exc),
                )
            )
            continue
        if value != "plot-rag-delta/v4":
            issues.append(
                GateIssue(
                    "V15_ADAPTER_CONTRACT_MISMATCH",
                    relative,
                    (
                        f"{constant_name} must be plot-rag-delta/v4; "
                        f"found {value!r}"
                    ),
                )
            )

    for (
        relative,
        function_name,
    ), (
        required_calls,
        required_names,
    ) in V15_ADAPTER_FUNCTION_CONTRACTS.items():
        tree = trees.get(relative)
        if tree is None:
            continue
        function = _top_level_function(tree, function_name)
        if function is None:
            issues.append(
                GateIssue(
                    "V15_ADAPTER_CONTRACT_MISMATCH",
                    relative,
                    f"required adapter function is missing: {function_name}",
                )
            )
            continue
        calls, names = _function_contract_surface(function)
        missing_calls = sorted(required_calls - calls)
        missing_names = sorted(required_names - names)
        if missing_calls or missing_names:
            issues.append(
                GateIssue(
                    "V15_ADAPTER_CONTRACT_MISMATCH",
                    relative,
                    (
                        f"{function_name} is disconnected; "
                        f"missing_calls={missing_calls!r}, "
                        f"missing_names={missing_names!r}"
                    ),
                )
            )
    return issues


def _validate_v15_payload_membership(root: Path) -> list[GateIssue]:
    try:
        entries = _tracked_paths(root)
    except RuntimeError as exc:
        return [
            GateIssue(
                "V15_PAYLOAD_INDEX_UNAVAILABLE",
                ".git",
                str(exc),
            )
        ]
    staged_payload = {
        entry.path
        for entry in entries
        if entry.stage == 0
        and not _is_noise(entry.path)
        and _is_allowed_payload_file(entry.path)
    }
    return [
        GateIssue(
            "V15_PAYLOAD_MEMBER_MISSING",
            relative,
            (
                "required v1.5 schema or adapter is not a stage-0 tracked "
                "release payload member"
            ),
        )
        for relative in V15_REQUIRED_PAYLOAD_PATHS
        if relative not in staged_payload
    ]


def _validate_v15_contract(
    root: Path,
    config_v3: Mapping[str, Any],
) -> list[GateIssue]:
    """Lock the additive v1.5 defaults and schema compatibility surface.

    The optimized prepare path, asynchronous extraction, and strict item
    runtime validation intentionally remain opt-in or shadowed.  Release
    validation therefore checks both the new capabilities and their safe
    compatibility defaults instead of merely checking that the template is
    syntactically valid JSON.
    """

    issues: list[GateIssue] = []
    for path, expected in V15_CONFIG_DEFAULTS.items():
        present, actual = _mapping_path_value(config_v3, path)
        if not present or type(actual) is not type(expected) or actual != expected:
            issues.append(
                GateIssue(
                    "V15_CONFIG_CONTRACT_INVALID",
                    "templates/config.v3.json",
                    (
                        f"{'.'.join(path)} must default to {expected!r}; "
                        f"found {actual!r}" if present else
                        f"{'.'.join(path)} is required"
                    ),
                )
            )

    schema_path = root / "scripts" / "continuity" / "schema.py"
    try:
        continuity_schema = _python_literal_constant(
            schema_path,
            "SCHEMA_VERSION",
        )
        item_projection_schema = _python_literal_constant(
            schema_path,
            "ITEM_PROJECTION_SCHEMA_VERSION",
        )
        event_types = _python_literal_constant(schema_path, "EVENT_TYPES")
    except (OSError, SyntaxError, ValueError) as exc:
        issues.append(
            GateIssue(
                "V15_CONTINUITY_SCHEMA_INVALID",
                "scripts/continuity/schema.py",
                str(exc),
            )
        )
        continuity_schema = None
        item_projection_schema = None
        event_types = None

    if continuity_schema != 7:
        issues.append(
            GateIssue(
                "V15_CONTINUITY_SCHEMA_MISMATCH",
                "scripts/continuity/schema.py",
                f"SCHEMA_VERSION must be 7; found {continuity_schema!r}",
            )
        )
    if item_projection_schema != 1:
        issues.append(
            GateIssue(
                "V15_ITEM_PROJECTION_SCHEMA_MISMATCH",
                "scripts/continuity/schema.py",
                (
                    "ITEM_PROJECTION_SCHEMA_VERSION must be 1; "
                    f"found {item_projection_schema!r}"
                ),
            )
        )
    normalized_event_types = (
        frozenset(str(value) for value in event_types)
        if isinstance(event_types, (tuple, list, set, frozenset))
        else frozenset()
    )
    missing_item_events = sorted(V15_ITEM_EVENT_TYPES - normalized_event_types)
    missing_legacy_events = sorted(
        V15_LEGACY_EVENT_TYPES - normalized_event_types
    )
    if missing_item_events or missing_legacy_events:
        issues.append(
            GateIssue(
                "V15_DELTA_COMPATIBILITY_MISMATCH",
                "scripts/continuity/schema.py",
                (
                    "schema v7 must retain legacy inventory events and the "
                    "v4 item event family; missing="
                    f"{missing_legacy_events + missing_item_events!r}"
                ),
            )
        )

    issues.extend(_validate_v15_schema_surface(root))
    issues.extend(_validate_v15_adapter_surface(root))
    return issues


def _ast_string_literals(node: ast.AST | None) -> frozenset[str]:
    if node is None:
        return frozenset()
    return frozenset(
        str(child.value)
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    )


def _call_target_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _validate_advantage_v1_payload_membership(root: Path) -> list[GateIssue]:
    required = (
        (
            "schemas/plot-rag-advantage/v1.schema.json",
            "schemas/plot-rag-advantage.v1.json",
        )
        + ADVANTAGE_V1_REQUIRED_SOURCE_PATHS
    )
    try:
        entries = _tracked_paths(root)
    except RuntimeError as exc:
        return [
            GateIssue(
                "ADVANTAGE_V1_PAYLOAD_INDEX_UNAVAILABLE",
                ".git",
                str(exc),
            )
        ]
    staged_payload = {
        entry.path
        for entry in entries
        if entry.stage == 0
        and not _is_noise(entry.path)
        and _is_allowed_payload_file(entry.path)
    }
    return [
        GateIssue(
            "ADVANTAGE_V1_PAYLOAD_MEMBER_MISSING",
            relative,
            (
                "required Advantage v1 schema, profile, adapter, query, "
                "migration, or acceptance surface is not a stage-0 tracked "
                "release payload member"
            ),
        )
        for relative in required
        if relative not in staged_payload
    ]


def _validate_advantage_v1_contract(
    root: Path,
    config_v3: Mapping[str, Any],
) -> list[GateIssue]:
    """Lock the independently hashed Advantage v1 release surface."""

    issues: list[GateIssue] = []
    for path, expected in {
        ("advantage", "enabled"): False,
        ("advantage", "shadow"): True,
        ("advantage", "schema_version"): "plot-rag-advantage/v1",
        ("advantage", "strict_runtime_validation"): False,
        ("advantage", "readable_projection"): True,
        ("advantage", "mandatory_context"): True,
    }.items():
        present, actual = _mapping_path_value(config_v3, path)
        if not present or type(actual) is not type(expected) or actual != expected:
            issues.append(
                GateIssue(
                    "ADVANTAGE_V1_CONFIG_CONTRACT_INVALID",
                    "templates/config.v3.json",
                    (
                        f"{'.'.join(path)} must default to {expected!r}; "
                        f"found {actual!r}"
                        if present
                        else f"{'.'.join(path)} is required"
                    ),
                )
            )

    for relative in ADVANTAGE_V1_REQUIRED_SOURCE_PATHS:
        if not _is_allowed_payload_file(relative):
            issues.append(
                GateIssue(
                    "ADVANTAGE_V1_PAYLOAD_SURFACE_INVALID",
                    relative,
                    "required Advantage v1 source is outside the release payload allowlist",
                )
            )
            continue
        try:
            _repository_reference(root, relative, expect="file")
        except (OSError, RuntimeError, ValueError) as exc:
            issues.append(
                GateIssue(
                    "ADVANTAGE_V1_SOURCE_REQUIRED_MISSING",
                    relative,
                    str(exc),
                )
            )

    profile_relative = "templates/advantage_profiles.v1.json"
    profile_path = root / profile_relative
    try:
        profile_registry = _load_json(profile_path)
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(
            GateIssue(
                "ADVANTAGE_V1_PROFILE_REGISTRY_INVALID",
                profile_relative,
                str(exc),
            )
        )
        profile_registry = None
    if isinstance(profile_registry, Mapping):
        if (
            profile_registry.get("schema_version")
            != "plot-rag-advantage-profile-registry/v1"
        ):
            issues.append(
                GateIssue(
                    "ADVANTAGE_V1_PROFILE_REGISTRY_INVALID",
                    profile_relative,
                    (
                        "schema_version must be "
                        "plot-rag-advantage-profile-registry/v1"
                    ),
                )
            )
        profiles = profile_registry.get("profiles")
        if not isinstance(profiles, list):
            issues.append(
                GateIssue(
                    "ADVANTAGE_V1_PROFILE_REGISTRY_INVALID",
                    profile_relative,
                    "profiles must be an array",
                )
            )
        else:
            profile_names = [
                str(profile.get("profile") or "")
                for profile in profiles
                if isinstance(profile, Mapping)
            ]
            profile_set = frozenset(profile_names)
            if (
                len(profile_names) != len(profile_set)
                or profile_set != ADVANTAGE_V1_REQUIRED_PROFILES
            ):
                issues.append(
                    GateIssue(
                        "ADVANTAGE_V1_PROFILE_SET_MISMATCH",
                        profile_relative,
                        (
                            "the registry must contain each of the 16 frozen "
                            "Advantage profiles exactly once; missing="
                            f"{sorted(ADVANTAGE_V1_REQUIRED_PROFILES - profile_set)!r}, "
                            "unexpected="
                            f"{sorted(profile_set - ADVANTAGE_V1_REQUIRED_PROFILES)!r}"
                        ),
                    )
                )
            required_profile_fields = frozenset(
                {
                    "profile",
                    "display_name",
                    "upper_classes",
                    "anchor_types",
                    "module_kinds",
                    "runtime_dimensions",
                    "ledger_entry_kinds",
                    "knowledge_requirements",
                    "contract_kinds",
                    "narrative_contract",
                    "compatibility",
                }
            )
            for index, profile in enumerate(profiles):
                if not isinstance(profile, Mapping):
                    issues.append(
                        GateIssue(
                            "ADVANTAGE_V1_PROFILE_REGISTRY_INVALID",
                            profile_relative,
                            f"profiles[{index}] must be an object",
                        )
                    )
                    continue
                missing = sorted(required_profile_fields - set(profile))
                if missing:
                    issues.append(
                        GateIssue(
                            "ADVANTAGE_V1_PROFILE_REGISTRY_INVALID",
                            profile_relative,
                            (
                                f"profile {profile.get('profile')!r} is "
                                f"missing fields {missing!r}"
                            ),
                        )
                    )
    elif profile_registry is not None:
        issues.append(
            GateIssue(
                "ADVANTAGE_V1_PROFILE_REGISTRY_INVALID",
                profile_relative,
                "registry root must be an object",
            )
        )

    try:
        sidecar_schema = _python_string_constant(
            root / "scripts" / "plot_init" / "advantages.py",
            "ADVANTAGE_SCHEMA_VERSION",
        )
        sidecar_path = _python_string_constant(
            root / "scripts" / "plot_init" / "advantages.py",
            "ADVANTAGE_SIDECAR_PATH",
        )
        projection_schema = _python_literal_constant(
            root / "scripts" / "continuity" / "advantages.py",
            "ADVANTAGE_PROJECTION_SCHEMA_VERSION",
        )
    except (OSError, SyntaxError, ValueError) as exc:
        issues.append(
            GateIssue(
                "ADVANTAGE_V1_SIDECAR_CONTRACT_INVALID",
                "scripts/plot_init/advantages.py",
                str(exc),
            )
        )
        sidecar_schema = None
        sidecar_path = None
        projection_schema = None
    if (
        sidecar_schema != "plot-rag-advantage/v1"
        or sidecar_path != ".plot-rag/advantages.v1.json"
        or projection_schema != 1
    ):
        issues.append(
            GateIssue(
                "ADVANTAGE_V1_SIDECAR_CONTRACT_MISMATCH",
                "scripts/plot_init/advantages.py",
                (
                    "sidecar must use .plot-rag/advantages.v1.json, "
                    "plot-rag-advantage/v1, and projection schema 1; found "
                    f"path={sidecar_path!r}, schema={sidecar_schema!r}, "
                    f"projection={projection_schema!r}"
                ),
            )
        )

    cli_relative = "scripts/plot_state.py"
    try:
        cli_tree = ast.parse(
            (root / cli_relative).read_text(encoding="utf-8"),
            filename=cli_relative,
        )
    except (OSError, SyntaxError) as exc:
        issues.append(
            GateIssue("ADVANTAGE_V1_CLI_CONTRACT_INVALID", cli_relative, str(exc))
        )
    else:
        cli_path = root / cli_relative
        try:
            cli_visibility_modes = _python_literal_constant(
                cli_path,
                "ADVANTAGE_VISIBILITIES",
            )
        except (OSError, SyntaxError, ValueError) as exc:
            cli_visibility_modes = None
            issues.append(
                GateIssue(
                    "ADVANTAGE_V1_VISIBILITY_CONTRACT_INVALID",
                    cli_relative,
                    str(exc),
                )
            )
        parser_function = _top_level_function(cli_tree, "_parser")
        parser_literals = _ast_string_literals(
            parser_function
        )
        dispatch_function = _top_level_function(cli_tree, "_dispatch")
        dispatch_literals = _ast_string_literals(dispatch_function)
        missing_parsers = sorted(
            ADVANTAGE_V1_REQUIRED_CLI_PARSERS - parser_literals
        )
        missing_helpers = sorted(
            ADVANTAGE_V1_REQUIRED_QUERY_HELPERS - dispatch_literals
        )
        parser_calls = (
            _function_contract_surface(parser_function)[0]
            if parser_function is not None
            else frozenset()
        )
        visibility_helper = _top_level_function(
            cli_tree,
            "_add_advantage_knowledge_scope",
        )
        visibility_option_seen = False
        visibility_choices_name = ""
        visibility_default: Any = None
        if visibility_helper is not None:
            for call in (
                node
                for node in ast.walk(visibility_helper)
                if isinstance(node, ast.Call)
            ):
                if _call_target_name(call) != "add_argument" or not call.args:
                    continue
                first = call.args[0]
                if not (
                    isinstance(first, ast.Constant)
                    and first.value == "--visibility"
                ):
                    continue
                visibility_option_seen = True
                for keyword in call.keywords:
                    if keyword.arg == "choices" and isinstance(
                        keyword.value,
                        ast.Name,
                    ):
                        visibility_choices_name = keyword.value.id
                    elif keyword.arg == "default":
                        try:
                            visibility_default = ast.literal_eval(keyword.value)
                        except (ValueError, TypeError):
                            visibility_default = None
                break
        cli_visibility_ok = (
            tuple(cli_visibility_modes or ())
            == ADVANTAGE_V1_VISIBILITY_MODES
            and visibility_option_seen
            and visibility_choices_name == "ADVANTAGE_VISIBILITIES"
            and visibility_default == ADVANTAGE_V1_VISIBILITY_DEFAULT
            and "_add_advantage_knowledge_scope" in parser_calls
            and "visibility" in dispatch_literals
        )
        if missing_parsers or missing_helpers or not cli_visibility_ok:
            issues.append(
                GateIssue(
                    "ADVANTAGE_V1_CLI_CONTRACT_MISMATCH",
                    cli_relative,
                    (
                        f"missing_parsers={missing_parsers!r}, "
                        f"missing_query_helpers={missing_helpers!r}, "
                        "visibility_contract="
                        f"{cli_visibility_ok!r}, modes="
                        f"{cli_visibility_modes!r}, default="
                        f"{visibility_default!r}"
                    ),
                )
            )

    mcp_relative = "scripts/plot_rag_mcp.py"
    try:
        mcp_tree = ast.parse(
            (root / mcp_relative).read_text(encoding="utf-8"),
            filename=mcp_relative,
        )
    except (OSError, SyntaxError) as exc:
        issues.append(
            GateIssue("ADVANTAGE_V1_MCP_CONTRACT_INVALID", mcp_relative, str(exc))
        )
    else:
        try:
            mcp_visibility = _python_literal_constant(
                root / mcp_relative,
                "ADVANTAGE_VISIBILITY",
            )
        except (OSError, SyntaxError, ValueError) as exc:
            mcp_visibility = None
            issues.append(
                GateIssue(
                    "ADVANTAGE_V1_VISIBILITY_CONTRACT_INVALID",
                    mcp_relative,
                    str(exc),
                )
            )
        tool_contracts: dict[str, bool] = {}
        visibility_scope_tools: set[str] = set()
        for call in (
            node for node in ast.walk(mcp_tree) if isinstance(node, ast.Call)
        ):
            if _call_target_name(call) != "_tool" or not call.args:
                continue
            first = call.args[0]
            if not (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
            ):
                continue
            read_only = False
            for keyword in call.keywords:
                if keyword.arg != "read_only":
                    continue
                try:
                    read_only = ast.literal_eval(keyword.value) is True
                except (ValueError, TypeError):
                    read_only = False
            tool_contracts[str(first.value)] = read_only
            if first.value in {
                "query_advantage_knowledge",
                "query_special_item_context",
            } and any(
                isinstance(node, ast.Name)
                and node.id == "ADVANTAGE_KNOWLEDGE_SCOPE"
                for node in ast.walk(call)
            ):
                visibility_scope_tools.add(str(first.value))
        missing_tools = sorted(
            ADVANTAGE_V1_REQUIRED_MCP_TOOLS - set(tool_contracts)
        )
        mutable_tools = sorted(
            tool
            for tool in ADVANTAGE_V1_REQUIRED_MCP_TOOLS
            if tool in tool_contracts and not tool_contracts[tool]
        )
        dispatch_literals = _ast_string_literals(
            _top_level_function(mcp_tree, "_dispatch_tool")
        )
        missing_dispatch = sorted(
            ADVANTAGE_V1_REQUIRED_MCP_TOOLS - dispatch_literals
        )
        expected_visibility_tools = {
            "query_advantage_knowledge",
            "query_special_item_context",
        }
        mcp_visibility_ok = (
            isinstance(mcp_visibility, Mapping)
            and mcp_visibility.get("type") == "string"
            and tuple(mcp_visibility.get("enum") or ())
            == ADVANTAGE_V1_VISIBILITY_MODES
            and mcp_visibility.get("default")
            == ADVANTAGE_V1_VISIBILITY_DEFAULT
            and visibility_scope_tools == expected_visibility_tools
            and "visibility" in dispatch_literals
            and ADVANTAGE_V1_VISIBILITY_DEFAULT in dispatch_literals
        )
        if (
            missing_tools
            or mutable_tools
            or missing_dispatch
            or not mcp_visibility_ok
        ):
            issues.append(
                GateIssue(
                    "ADVANTAGE_V1_MCP_CONTRACT_MISMATCH",
                    mcp_relative,
                    (
                        f"missing_tools={missing_tools!r}, "
                        f"non_read_only={mutable_tools!r}, "
                        f"missing_dispatch={missing_dispatch!r}, "
                        "visibility_contract="
                        f"{mcp_visibility_ok!r}, schema="
                        f"{mcp_visibility!r}, scoped_tools="
                        f"{sorted(visibility_scope_tools)!r}"
                    ),
                )
            )

    hook_relative = "hooks/plot_progression_gate.py"
    runtime_relative = "scripts/v1_runtime.py"
    hook_literals: frozenset[str] = frozenset()
    runtime_literals: frozenset[str] = frozenset()
    for relative, target in (
        (hook_relative, "hook"),
        (runtime_relative, "runtime"),
    ):
        try:
            tree = ast.parse(
                (root / relative).read_text(encoding="utf-8"),
                filename=relative,
            )
        except (OSError, SyntaxError) as exc:
            issues.append(
                GateIssue(
                    "ADVANTAGE_V1_HOOK_CONTEXT_INVALID",
                    relative,
                    str(exc),
                )
            )
            continue
        if target == "hook":
            hook_literals = _ast_string_literals(tree)
        else:
            runtime_literals = _ast_string_literals(tree)
    if (
        not any("[PLOT_RAG_ADVANTAGE_HOOK]" in value for value in hook_literals)
        or not any(
            "post_locked_intent_and_event_experience_prepare" in value
            for value in hook_literals
        )
        or not any(
            "[ACCEPTED_ADVANTAGE_CONTEXT]" in value
            for value in runtime_literals
        )
        or not any("advantage_context" in value for value in runtime_literals)
    ):
        issues.append(
            GateIssue(
                "ADVANTAGE_V1_HOOK_CONTEXT_MISMATCH",
                hook_relative,
                (
                    "Hook and prepare must expose the frozen Advantage markers "
                    "after locked intent and event-experience preparation"
                ),
            )
        )

    migration_relative = "scripts/v1_runtime.py"
    try:
        migration_tree = ast.parse(
            (root / migration_relative).read_text(encoding="utf-8"),
            filename=migration_relative,
        )
    except (OSError, SyntaxError) as exc:
        issues.append(
            GateIssue(
                "ADVANTAGE_V1_MIGRATION_CONTRACT_INVALID",
                migration_relative,
                str(exc),
            )
        )
    else:
        migration_function = _top_level_function(
            migration_tree,
            "migrate_state_schema",
        )
        calls, names = (
            _function_contract_surface(migration_function)
            if migration_function is not None
            else (frozenset(), frozenset())
        )
        literals = _ast_string_literals(migration_function)
        required_calls = frozenset(
            {
                "read_item_projection_metadata",
                "read_advantage_projection_metadata",
            }
        )
        required_names = frozenset(
            {
                "ITEM_PROJECTION_META_HASH",
                "ADVANTAGE_META_HASH",
            }
        )
        required_literals = frozenset(
            {
                "item_projection_hash",
                "advantage_projection_hash",
                "readable_projection_cleanup",
            }
        )
        if (
            migration_function is None
            or not required_calls.issubset(calls)
            or not required_names.issubset(names)
            or not required_literals.issubset(literals)
        ):
            issues.append(
                GateIssue(
                    "ADVANTAGE_V1_MIGRATION_CONTRACT_MISMATCH",
                    migration_relative,
                    (
                        "state migration receipt must bind item and Advantage "
                        "projection hashes and publish deterministic readable "
                        "projection cleanup instructions"
                    ),
                )
            )

    return issues


def _validate_source_manifest_contract(root: Path) -> list[GateIssue]:
    """Freeze the post-initialization source-manifest lifecycle surface."""

    issues: list[GateIssue] = []
    missing_paths = [
        relative
        for relative in SOURCE_MANIFEST_REQUIRED_SOURCE_PATHS
        if not (root / relative).is_file()
    ]
    if missing_paths:
        issues.append(
            GateIssue(
                "SOURCE_MANIFEST_PAYLOAD_MISSING",
                "scripts/continuity/source_manifest.py",
                f"missing required source-manifest paths: {missing_paths!r}",
            )
        )
        return issues

    source_relative = "scripts/continuity/source_manifest.py"
    source_path = root / source_relative
    try:
        source_tree = ast.parse(
            source_path.read_text(encoding="utf-8"),
            filename=source_relative,
        )
        plan_schema = _python_literal_constant(
            source_path,
            "SOURCE_MANIFEST_PLAN_SCHEMA",
        )
        change_schema = _python_literal_constant(
            source_path,
            "SOURCE_MANIFEST_CHANGE_SCHEMA",
        )
        proposal_kind = _python_literal_constant(
            source_path,
            "SOURCE_MANIFEST_PROPOSAL_KIND",
        )
        accept_operation = _python_literal_constant(
            source_path,
            "SOURCE_MANIFEST_ACCEPT_OPERATION",
        )
        artifact_id = _python_literal_constant(
            source_path,
            "SOURCE_MANIFEST_ARTIFACT_ID",
        )
        artifact_kind = _python_literal_constant(
            source_path,
            "SOURCE_MANIFEST_ARTIFACT_KIND",
        )
        artifact_stage = _python_literal_constant(
            source_path,
            "SOURCE_MANIFEST_ARTIFACT_STAGE",
        )
        branch_id = _python_literal_constant(
            source_path,
            "SOURCE_MANIFEST_BRANCH_ID",
        )
    except (OSError, SyntaxError, ValueError) as exc:
        issues.append(
            GateIssue(
                "SOURCE_MANIFEST_CORE_INVALID",
                source_relative,
                str(exc),
            )
        )
        return issues
    required_source_functions = {
        "preview_manifest_plan",
        "validate_frozen_manifest_change",
        "current_manifest_snapshot",
        "replay_source_manifest",
        "manifest_status",
        "validate_source_manifest_proposal_envelope",
    }
    source_functions = {
        node.name
        for node in source_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    core_contract_ok = (
        plan_schema == "plot-rag-source-manifest-migration-plan/v1"
        and change_schema == "plot-rag-source-manifest/v1"
        and proposal_kind == "source_manifest_change"
        and accept_operation == "accept_source_manifest"
        and artifact_id == "plot_rag_source_manifest"
        and artifact_kind == "source_manifest"
        and artifact_stage == "bootstrap"
        and branch_id == "main"
        and required_source_functions.issubset(source_functions)
    )
    if not core_contract_ok:
        issues.append(
            GateIssue(
                "SOURCE_MANIFEST_CORE_MISMATCH",
                source_relative,
                (
                    f"plan_schema={plan_schema!r}, change_schema={change_schema!r}, "
                    f"proposal_kind={proposal_kind!r}, "
                    f"accept_operation={accept_operation!r}, "
                    f"artifact_id={artifact_id!r}, "
                    f"artifact_kind={artifact_kind!r}, "
                    f"artifact_stage={artifact_stage!r}, "
                    f"branch_id={branch_id!r}, "
                    "missing_functions="
                    f"{sorted(required_source_functions - source_functions)!r}"
                ),
            )
        )

    runtime_relative = "scripts/v1_runtime.py"
    cli_relative = "scripts/plot_state.py"
    try:
        runtime_tree = ast.parse(
            (root / runtime_relative).read_text(encoding="utf-8"),
            filename=runtime_relative,
        )
        cli_tree = ast.parse(
            (root / cli_relative).read_text(encoding="utf-8"),
            filename=cli_relative,
        )
    except (OSError, SyntaxError) as exc:
        issues.append(
            GateIssue(
                "SOURCE_MANIFEST_CLI_INVALID",
                cli_relative,
                str(exc),
            )
        )
    else:
        runtime_functions = {
            node.name
            for node in runtime_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        parser_literals = _ast_string_literals(
            _top_level_function(cli_tree, "_parser")
        )
        dispatch_literals = _ast_string_literals(
            _top_level_function(cli_tree, "_dispatch")
        )
        required_cli_literals = {
            "source-manifest",
            "status",
            "preview",
            "propose",
        }
        missing_runtime = sorted(
            SOURCE_MANIFEST_REQUIRED_RUNTIME_HELPERS - runtime_functions
        )
        missing_parser = sorted(required_cli_literals - parser_literals)
        missing_dispatch = sorted(required_cli_literals - dispatch_literals)
        if missing_runtime or missing_parser or missing_dispatch:
            issues.append(
                GateIssue(
                    "SOURCE_MANIFEST_CLI_CONTRACT_MISMATCH",
                    cli_relative,
                    (
                        f"missing_runtime={missing_runtime!r}, "
                        f"missing_parser={missing_parser!r}, "
                        f"missing_dispatch={missing_dispatch!r}"
                    ),
                )
            )

    mcp_relative = "scripts/plot_rag_mcp.py"
    try:
        mcp_tree = ast.parse(
            (root / mcp_relative).read_text(encoding="utf-8"),
            filename=mcp_relative,
        )
    except (OSError, SyntaxError) as exc:
        issues.append(
            GateIssue(
                "SOURCE_MANIFEST_MCP_INVALID",
                mcp_relative,
                str(exc),
            )
        )
    else:
        tool_contracts: dict[str, bool] = {}
        for call in (
            node for node in ast.walk(mcp_tree) if isinstance(node, ast.Call)
        ):
            if _call_target_name(call) != "_tool" or not call.args:
                continue
            first = call.args[0]
            if not (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value in SOURCE_MANIFEST_REQUIRED_MCP_TOOLS
            ):
                continue
            read_only = False
            for keyword in call.keywords:
                if keyword.arg != "read_only":
                    continue
                try:
                    read_only = ast.literal_eval(keyword.value) is True
                except (ValueError, TypeError):
                    read_only = False
            tool_contracts[str(first.value)] = read_only
        dispatch_literals = _ast_string_literals(
            _top_level_function(mcp_tree, "_dispatch_tool")
        )
        missing_tools = sorted(
            set(SOURCE_MANIFEST_REQUIRED_MCP_TOOLS) - set(tool_contracts)
        )
        wrong_annotations = sorted(
            name
            for name, expected_read_only in (
                SOURCE_MANIFEST_REQUIRED_MCP_TOOLS.items()
            )
            if name in tool_contracts
            and tool_contracts[name] is not expected_read_only
        )
        missing_dispatch = sorted(
            set(SOURCE_MANIFEST_REQUIRED_MCP_TOOLS) - dispatch_literals
        )
        forbidden_issuer_names = {
            "HostApprovalAuthority",
            "issue_host_approval",
        }
        observed_names = {
            node.id for node in ast.walk(mcp_tree) if isinstance(node, ast.Name)
        } | {
            node.attr
            for node in ast.walk(mcp_tree)
            if isinstance(node, ast.Attribute)
        }
        issuer_names = sorted(forbidden_issuer_names & observed_names)
        if (
            missing_tools
            or wrong_annotations
            or missing_dispatch
            or issuer_names
        ):
            issues.append(
                GateIssue(
                    "SOURCE_MANIFEST_MCP_CONTRACT_MISMATCH",
                    mcp_relative,
                    (
                        f"missing_tools={missing_tools!r}, "
                        f"wrong_annotations={wrong_annotations!r}, "
                        f"missing_dispatch={missing_dispatch!r}, "
                        f"grant_issuer_names={issuer_names!r}"
                    ),
                )
            )
    return issues


def _validate_power_spec_payload_membership(root: Path) -> list[GateIssue]:
    """Require every standalone PowerSpec entrypoint in the staged payload."""

    try:
        entries = _tracked_paths(root)
    except RuntimeError as exc:
        return [
            GateIssue(
                "POWER_SPEC_PAYLOAD_INDEX_UNAVAILABLE",
                ".git",
                str(exc),
            )
        ]
    staged_payload = {
        entry.path
        for entry in entries
        if entry.stage == 0
        and not _is_noise(entry.path)
        and _is_allowed_payload_file(entry.path)
    }
    return [
        GateIssue(
            "POWER_SPEC_PAYLOAD_MEMBER_MISSING",
            relative,
            (
                "required standalone PowerSpec compiler, integration, or "
                "regression-test surface is not a stage-0 tracked release "
                "payload member"
            ),
        )
        for relative in POWER_SPEC_REQUIRED_SOURCE_PATHS
        if relative not in staged_payload
    ]


def _validate_power_spec_contract(root: Path) -> list[GateIssue]:
    """Freeze the standalone PowerSpec lifecycle and public catalog."""

    issues: list[GateIssue] = []
    missing_paths = [
        relative
        for relative in POWER_SPEC_REQUIRED_SOURCE_PATHS
        if not (root / relative).is_file()
    ]
    if missing_paths:
        issues.append(
            GateIssue(
                "POWER_SPEC_PAYLOAD_MISSING",
                "scripts/continuity/power_spec.py",
                f"missing required standalone PowerSpec paths: {missing_paths!r}",
            )
        )
        return issues

    core_relative = "scripts/continuity/power_spec.py"
    core_path = root / core_relative
    try:
        core_tree = ast.parse(
            core_path.read_text(encoding="utf-8"),
            filename=core_relative,
        )
        lifecycle_schema = _python_literal_constant(
            core_path,
            "POWER_SPEC_LIFECYCLE_SCHEMA",
        )
        proposal_kind = _python_literal_constant(
            core_path,
            "POWER_SPEC_PROPOSAL_KIND",
        )
        required_operation = _python_literal_constant(
            core_path,
            "POWER_SPEC_REQUIRED_OPERATION",
        )
        scope = _python_literal_constant(
            core_path,
            "POWER_SPEC_SCOPE",
        )
        artifact_stage = _python_literal_constant(
            core_path,
            "POWER_SPEC_ARTIFACT_STAGE",
        )
        collections = _python_literal_constant(
            core_path,
            "POWER_SPEC_COLLECTIONS",
        )
    except (OSError, SyntaxError, ValueError) as exc:
        issues.append(
            GateIssue(
                "POWER_SPEC_CORE_INVALID",
                core_relative,
                str(exc),
            )
        )
        return issues

    core_functions = {
        node.name
        for node in core_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    core_contract_ok = (
        lifecycle_schema == "plot-rag-lifecycle/power-spec-package-v1"
        and proposal_kind == "power_spec_change"
        and required_operation == "accept_power_spec"
        and scope == "timeless"
        and artifact_stage == "bootstrap"
        and collections == POWER_SPEC_COLLECTIONS_CONTRACT
        and POWER_SPEC_REQUIRED_CORE_FUNCTIONS.issubset(core_functions)
    )
    if not core_contract_ok:
        issues.append(
            GateIssue(
                "POWER_SPEC_CORE_MISMATCH",
                core_relative,
                (
                    f"lifecycle_schema={lifecycle_schema!r}, "
                    f"proposal_kind={proposal_kind!r}, "
                    f"required_operation={required_operation!r}, "
                    f"scope={scope!r}, artifact_stage={artifact_stage!r}, "
                    f"collections_match="
                    f"{collections == POWER_SPEC_COLLECTIONS_CONTRACT!r}, "
                    "missing_functions="
                    f"{sorted(POWER_SPEC_REQUIRED_CORE_FUNCTIONS - core_functions)!r}"
                ),
            )
        )

    core_function_calls = {
        name: (
            _function_contract_surface(function)[0]
            if (
                function := _top_level_function(core_tree, name)
            ) is not None
            else frozenset()
        )
        for name in POWER_SPEC_REQUIRED_CORE_FUNCTIONS
    }
    required_core_calls = {
        "validate_power_spec_import": {
            "normalize_power_spec_import",
        },
        "build_power_spec_lifecycle_package": {
            "normalize_power_spec_import",
            "_compile_normalized",
            "validate_power_spec_lifecycle_package",
        },
        "compile_power_spec_change": {
            "build_power_spec_lifecycle_package",
        },
        "preview_power_spec_import": {
            "normalize_power_spec_import",
            "_compile_normalized",
            "validate_power_spec_lifecycle_package",
        },
    }
    missing_core_calls = {
        name: sorted(required - core_function_calls.get(name, frozenset()))
        for name, required in required_core_calls.items()
        if not required.issubset(
            core_function_calls.get(name, frozenset())
        )
    }
    preview_core = _top_level_function(
        core_tree,
        "preview_power_spec_import",
    )
    preview_literals = _ast_string_literals(preview_core)
    if (
        missing_core_calls
        or "read_only" not in preview_literals
        or "ready" not in preview_literals
    ):
        issues.append(
            GateIssue(
                "POWER_SPEC_COMPILER_CONTRACT_MISMATCH",
                core_relative,
                (
                    f"missing_calls={missing_core_calls!r}, "
                    "preview_read_only_marker="
                    f"{'read_only' in preview_literals!r}, "
                    f"preview_ready_marker={'ready' in preview_literals!r}"
                ),
            )
        )

    service_relative = "scripts/continuity/service.py"
    runtime_relative = "scripts/v1_runtime.py"
    cli_relative = "scripts/plot_state.py"
    try:
        service_tree = ast.parse(
            (root / service_relative).read_text(encoding="utf-8"),
            filename=service_relative,
        )
        runtime_tree = ast.parse(
            (root / runtime_relative).read_text(encoding="utf-8"),
            filename=runtime_relative,
        )
        cli_tree = ast.parse(
            (root / cli_relative).read_text(encoding="utf-8"),
            filename=cli_relative,
        )
    except (OSError, SyntaxError) as exc:
        issues.append(
            GateIssue(
                "POWER_SPEC_CLI_INVALID",
                cli_relative,
                str(exc),
            )
        )
    else:
        service_class = next(
            (
                node
                for node in service_tree.body
                if isinstance(node, ast.ClassDef)
                and node.name == "ContinuityService"
            ),
            None,
        )
        service_methods = {
            node.name
            for node in (service_class.body if service_class else ())
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing_service = sorted(
            POWER_SPEC_REQUIRED_SERVICE_METHODS - service_methods
        )
        preview_service = next(
            (
                node
                for node in (service_class.body if service_class else ())
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "preview_power_spec_change"
            ),
            None,
        )
        propose_service = next(
            (
                node
                for node in (service_class.body if service_class else ())
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "propose_power_spec_change"
            ),
            None,
        )
        preview_service_names = (
            _function_contract_surface(preview_service)[1]
            if preview_service is not None
            else frozenset()
        )
        preview_service_attributes = {
            node.attr
            for node in ast.walk(preview_service)
            if isinstance(node, ast.Attribute)
        } if preview_service is not None else set()
        propose_service_names = (
            _function_contract_surface(propose_service)[1]
            if propose_service is not None
            else frozenset()
        )
        propose_service_attributes = {
            node.attr
            for node in ast.walk(propose_service)
            if isinstance(node, ast.Attribute)
        } if propose_service is not None else set()
        preview_forbidden = {
            "atomic_write",
            "register_entity",
            "save_proposal",
            "write_connection",
        } & preview_service_attributes
        service_contract_ok = (
            not missing_service
            and "preview_power_spec_import" in preview_service_names
            and "_open_private_database_snapshot" in preview_service_names
            and not preview_forbidden
            and "POWER_SPEC_PROPOSAL_KIND" in propose_service_names
            and {
                "atomic_write",
                "get_meta_int",
                "register_entity",
                "save_proposal",
            }.issubset(propose_service_attributes)
            and "CANON_REVISION_CONFLICT"
            in _ast_string_literals(propose_service)
        )

        runtime_functions = {
            node.name
            for node in runtime_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing_runtime = sorted(
            POWER_SPEC_REQUIRED_RUNTIME_HELPERS - runtime_functions
        )
        parser_function = _top_level_function(cli_tree, "_parser")
        dispatch_function = _top_level_function(cli_tree, "_dispatch")
        parser_literals = _ast_string_literals(parser_function)
        dispatch_literals = _ast_string_literals(dispatch_function)
        dispatch_attributes = {
            node.attr
            for node in ast.walk(dispatch_function)
            if isinstance(node, ast.Attribute)
        } if dispatch_function is not None else set()
        missing_parser = sorted(
            POWER_SPEC_REQUIRED_CLI_LITERALS - parser_literals
        )
        missing_dispatch_literals = sorted(
            {"power-spec", "validate", "preview", "propose"}
            - dispatch_literals
        )
        missing_dispatch_helpers = sorted(
            POWER_SPEC_REQUIRED_RUNTIME_HELPERS - dispatch_attributes
        )
        if (
            not service_contract_ok
            or missing_runtime
            or missing_parser
            or missing_dispatch_literals
            or missing_dispatch_helpers
        ):
            issues.append(
                GateIssue(
                    "POWER_SPEC_CLI_CONTRACT_MISMATCH",
                    cli_relative,
                    (
                        f"missing_service={missing_service!r}, "
                        f"service_contract={service_contract_ok!r}, "
                        f"preview_forbidden={sorted(preview_forbidden)!r}, "
                        f"missing_runtime={missing_runtime!r}, "
                        f"missing_parser={missing_parser!r}, "
                        "missing_dispatch_literals="
                        f"{missing_dispatch_literals!r}, "
                        "missing_dispatch_helpers="
                        f"{missing_dispatch_helpers!r}"
                    ),
                )
            )

    mcp_relative = "scripts/plot_rag_mcp.py"
    try:
        mcp_path = root / mcp_relative
        mcp_tree = ast.parse(
            mcp_path.read_text(encoding="utf-8"),
            filename=mcp_relative,
        )
        document_schema = _python_literal_constant(
            mcp_path,
            "POWER_SPEC_DOCUMENT",
        )
    except (OSError, SyntaxError, ValueError) as exc:
        issues.append(
            GateIssue(
                "POWER_SPEC_MCP_INVALID",
                mcp_relative,
                str(exc),
            )
        )
    else:
        tool_contracts: dict[
            str,
            tuple[bool, tuple[str, ...] | None],
        ] = {}
        for call in (
            node for node in ast.walk(mcp_tree) if isinstance(node, ast.Call)
        ):
            if _call_target_name(call) != "_tool" or not call.args:
                continue
            first = call.args[0]
            if not (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value in POWER_SPEC_REQUIRED_MCP_TOOLS
            ):
                continue
            read_only = False
            for keyword in call.keywords:
                if keyword.arg != "read_only":
                    continue
                try:
                    read_only = ast.literal_eval(keyword.value) is True
                except (ValueError, TypeError):
                    read_only = False
            required_fields: tuple[str, ...] | None = None
            if len(call.args) >= 4:
                try:
                    literal_required = ast.literal_eval(call.args[3])
                except (ValueError, TypeError):
                    literal_required = None
                if isinstance(literal_required, (tuple, list)):
                    required_fields = tuple(
                        str(value) for value in literal_required
                    )
            tool_contracts[str(first.value)] = (
                read_only,
                required_fields,
            )
        missing_tools = sorted(
            set(POWER_SPEC_REQUIRED_MCP_TOOLS) - set(tool_contracts)
        )
        mismatched_tools = sorted(
            name
            for name, expected in POWER_SPEC_REQUIRED_MCP_TOOLS.items()
            if name in tool_contracts and tool_contracts[name] != expected
        )
        dispatch_literals = _ast_string_literals(
            _top_level_function(mcp_tree, "_dispatch_tool")
        )
        missing_dispatch = sorted(
            set(POWER_SPEC_REQUIRED_MCP_TOOLS) - dispatch_literals
        )
        forbidden_issuer_names = {
            "HostApprovalAuthority",
            "issue_host_approval",
        }
        observed_names = {
            node.id for node in ast.walk(mcp_tree) if isinstance(node, ast.Name)
        } | {
            node.attr
            for node in ast.walk(mcp_tree)
            if isinstance(node, ast.Attribute)
        }
        issuer_names = sorted(forbidden_issuer_names & observed_names)
        expected_document_schema = {
            "type": "object",
            "minProperties": 1,
            "description": (
                "Complete plot-rag-power/v1 aggregate. Stable entities, "
                "lifecycle events, and proposal hashes are compiled locally."
            ),
        }
        if (
            document_schema != expected_document_schema
            or missing_tools
            or mismatched_tools
            or missing_dispatch
            or issuer_names
        ):
            issues.append(
                GateIssue(
                    "POWER_SPEC_MCP_CONTRACT_MISMATCH",
                    mcp_relative,
                    (
                        f"document_schema_match="
                        f"{document_schema == expected_document_schema!r}, "
                        f"missing_tools={missing_tools!r}, "
                        f"mismatched_tools={mismatched_tools!r}, "
                        f"missing_dispatch={missing_dispatch!r}, "
                        f"grant_issuer_names={issuer_names!r}"
                    ),
                )
            )

    test_relative = "tests/test_power_spec_import.py"
    lifecycle_test_relative = "tests/test_power_spec_lifecycle.py"
    try:
        test_tree = ast.parse(
            (root / test_relative).read_text(encoding="utf-8"),
            filename=test_relative,
        )
        lifecycle_test_tree = ast.parse(
            (root / lifecycle_test_relative).read_text(encoding="utf-8"),
            filename=lifecycle_test_relative,
        )
        cli_test_tree = ast.parse(
            (root / "tests" / "test_cli.py").read_text(encoding="utf-8"),
            filename="tests/test_cli.py",
        )
        mcp_test_tree = ast.parse(
            (root / "tests" / "test_mcp.py").read_text(encoding="utf-8"),
            filename="tests/test_mcp.py",
        )
    except (OSError, SyntaxError) as exc:
        issues.append(
            GateIssue(
                "POWER_SPEC_TEST_CONTRACT_INVALID",
                test_relative,
                str(exc),
            )
        )
    else:
        observed_tests = {
            node.name
            for node in ast.walk(test_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        }
        required_tests = {
            "test_build_is_deterministic_and_uses_stable_normalized_ids",
            "test_preview_is_read_only_and_exposes_normalized_aggregate",
            "test_actor_runtime_is_not_silently_discarded",
            "test_tampered_lifecycle_package_hash_is_rejected",
            "test_validate_and_preview_do_not_touch_project_files",
        }
        missing_tests = sorted(required_tests - observed_tests)
        observed_lifecycle_tests = {
            node.name
            for node in ast.walk(lifecycle_test_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        }
        required_lifecycle_tests = {
            "test_preview_without_state_fails_closed_without_creating_database",
            "test_validate_and_preview_are_read_only",
            "test_preview_rejects_foreign_database_without_byte_changes",
            "test_preview_rejects_partial_schema_without_byte_changes",
            "test_propose_is_atomic_when_proposal_save_fails",
            "test_entity_id_type_conflict_fails_closed",
            "test_revision_drift_between_preview_and_write_is_atomic",
            "test_propose_is_idempotent_and_does_not_issue_grant",
            "test_generic_accept_power_spec_and_replay_expose_queries",
        }
        missing_lifecycle_tests = sorted(
            required_lifecycle_tests - observed_lifecycle_tests
        )
        cli_test_literals = _ast_string_literals(
            cli_test_tree
        )
        mcp_test_literals = _ast_string_literals(
            mcp_test_tree
        )
        missing_cli_test_markers = sorted(
            {"power-spec", "validate", "preview", "propose"}
            - cli_test_literals
        )
        missing_mcp_test_markers = sorted(
            set(POWER_SPEC_REQUIRED_MCP_TOOLS) - mcp_test_literals
        )
        if (
            missing_tests
            or missing_lifecycle_tests
            or missing_cli_test_markers
            or missing_mcp_test_markers
        ):
            issues.append(
                GateIssue(
                    "POWER_SPEC_TEST_CONTRACT_MISMATCH",
                    test_relative,
                    (
                        f"missing_tests={missing_tests!r}, "
                        "missing_lifecycle_tests="
                        f"{missing_lifecycle_tests!r}, "
                        "missing_cli_markers="
                        f"{missing_cli_test_markers!r}, "
                        "missing_mcp_markers="
                        f"{missing_mcp_test_markers!r}"
                    ),
                )
            )
    return issues


def _skill_frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip().strip("\"'")
    return fields


def _lexical_relative_path(
    value: str,
    *,
    allow_root: bool = False,
) -> PurePosixPath:
    raw = str(value or "")
    windows = PureWindowsPath(raw)
    normalized = raw.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if windows.drive or windows.is_absolute() or relative.is_absolute():
        raise ValueError(f"path must be relative: {value!r}")
    if any(part == ".." for part in relative.parts):
        raise ValueError(f"path traversal is not allowed: {value!r}")
    if not relative.parts:
        if allow_root and raw in {".", "./", ".\\"}:
            return PurePosixPath(".")
        raise ValueError(f"path must not be empty: {value!r}")
    portability_problem = _portable_path_problem(relative.parts)
    if portability_problem is not None:
        raise ValueError(
            f"path is not portable across release platforms: "
            f"{portability_problem}"
        )
    return relative


def _portable_path_problem(parts: Iterable[str]) -> str | None:
    for part in parts:
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in part
        ):
            return f"component contains a control character: {part!r}"
        invalid = sorted(
            {
                character
                for character in part
                if character in WINDOWS_INVALID_PATH_CHARACTERS
            }
        )
        if invalid:
            return (
                f"component contains Windows-invalid characters "
                f"{''.join(invalid)!r}: {part!r}"
            )
        if part.endswith((" ", ".")):
            return f"component has a trailing dot or space: {part!r}"
        stem = part.split(".", 1)[0].casefold()
        if stem in WINDOWS_RESERVED_PATH_NAMES:
            return f"component uses reserved Windows device name: {part!r}"
    return None


def _unquote_scalar(value: str) -> str:
    stripped = str(value or "").strip()
    if (
        len(stripped) >= 2
        and stripped[0] == stripped[-1]
        and stripped[0] in {"'", '"'}
    ):
        return stripped[1:-1]
    return stripped


def _path_argument_value(value: str) -> str:
    candidate = _unquote_scalar(value)
    if candidate.startswith("-") and "=" in candidate:
        candidate = _unquote_scalar(candidate.split("=", 1)[1])
    return candidate.strip()


def _has_option_assignment(value: str) -> bool:
    candidate = _unquote_scalar(value)
    return candidate.startswith("-") and "=" in candidate


def _is_non_file_uri(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return (
        "://" in value
        and bool(parsed.scheme)
        and parsed.scheme.casefold() != "file"
    )


def _looks_like_repository_path(
    root: Path,
    value: str,
    *,
    base: str = ".",
    command: bool = False,
    fail_closed_separators: bool = False,
) -> bool:
    candidate = _path_argument_value(value)
    if not candidate or _is_non_file_uri(candidate):
        return False
    windows = PureWindowsPath(candidate)
    normalized = candidate.replace("\\", "/")
    posix = PurePosixPath(normalized)
    if (
        windows.drive
        or windows.is_absolute()
        or posix.is_absolute()
        or any(part == ".." for part in posix.parts)
        or candidate.startswith(("~/", "~\\"))
    ):
        return True
    if candidate.startswith(("./", ".\\", "../", "..\\")):
        return True
    if fail_closed_separators and ("/" in candidate or "\\" in candidate):
        return True
    if command and ("/" in candidate or "\\" in candidate):
        return True
    suffix = Path(normalized).suffix.casefold()
    if suffix in PATH_ARGUMENT_SUFFIXES and not (
        command and suffix == ".exe" and "/" not in normalized
    ):
        return True
    try:
        base_relative = _lexical_relative_path(base, allow_root=True)
        value_relative = _lexical_relative_path(candidate, allow_root=True)
    except ValueError:
        return True
    path = root.resolve().joinpath(
        *base_relative.parts,
        *value_relative.parts,
    )
    return path.exists() or path.is_symlink()


def _repository_path_argument(
    root: Path,
    value: str,
    *,
    base: str = ".",
) -> tuple[Path, str]:
    candidate = _path_argument_value(value)
    if candidate.startswith(("~/", "~\\")):
        raise ValueError(f"home-relative paths are not allowed: {value!r}")
    errors: list[Exception] = []
    for expect in ("file", "dir"):
        try:
            return _repository_reference(
                root,
                candidate,
                base=base,
                expect=expect,
                require_payload_file=expect == "file",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(exc)
    raise ValueError(str(errors[0]) if errors else f"invalid path: {value!r}")


def _shell_tokens(command: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(command, posix=True))
    except ValueError as exc:
        raise ValueError(f"command could not be tokenized: {exc}") from exc


def _is_python_command(value: str) -> bool:
    name = PureWindowsPath(_unquote_scalar(value)).name.casefold()
    return bool(
        re.fullmatch(
            r"(?:pythonw?|py)(?:\d+(?:\.\d+)*)?(?:\.exe)?",
            name,
        )
    )


def _python_bytecode_disabled(tokens: Sequence[str]) -> bool:
    return bool(
        tokens
        and _is_python_command(tokens[0])
        and len(tokens) > 1
        and tokens[1] == "-B"
    )


def _repository_reference(
    root: Path,
    value: str,
    *,
    base: str = ".",
    expect: str,
    require_payload_file: bool = False,
) -> tuple[Path, str]:
    root = root.resolve()
    base_relative = _lexical_relative_path(base, allow_root=True)
    value_relative = _lexical_relative_path(value, allow_root=expect == "dir")
    combined_parts = base_relative.parts + value_relative.parts
    combined = (
        PurePosixPath(*combined_parts)
        if combined_parts
        else PurePosixPath(".")
    )
    current = root
    final_stat = os.lstat(root)
    for part in combined.parts:
        current /= part
        try:
            final_stat = os.lstat(current)
        except OSError as exc:
            raise ValueError(f"referenced path is unavailable: {combined}") from exc
        if _is_link_or_reparse(final_stat):
            raise ValueError(
                f"referenced path contains a link or reparse point: {combined}"
            )
    resolved = current.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"referenced path escapes plugin root: {combined}")
    if expect == "file" and not stat.S_ISREG(final_stat.st_mode):
        raise ValueError(f"referenced path is not a regular file: {combined}")
    if expect == "dir" and not stat.S_ISDIR(final_stat.st_mode):
        raise ValueError(f"referenced path is not a directory: {combined}")
    relative = "." if resolved == root else resolved.relative_to(root).as_posix()
    if require_payload_file:
        if not _is_allowed_payload_file(relative):
            raise ValueError(
                f"referenced file is outside the payload allowlist: {relative}"
            )
        entries = {
            entry.path: entry
            for entry in _tracked_paths(root)
            if entry.stage == 0
        }
        entry = entries.get(relative)
        if entry is None:
            raise ValueError(f"referenced file is not staged in Git: {relative}")
        problem = _payload_path_problem(root, entry)
        if problem is not None:
            raise ValueError(problem.render())
    return resolved, relative


def _validate_manifest(root: Path) -> tuple[list[GateIssue], dict[str, Any]]:
    issues: list[GateIssue] = []
    try:
        manifest_path, _manifest_relative = _repository_reference(
            root,
            ".codex-plugin/plugin.json",
            expect="file",
            require_payload_file=True,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return [
            GateIssue(
                "PLUGIN_MANIFEST_MISSING",
                ".codex-plugin/plugin.json",
                f"required plugin manifest is unavailable: {exc}",
            )
        ], {}
    try:
        manifest = _load_json(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        return [
            GateIssue(
                "PLUGIN_MANIFEST_INVALID",
                ".codex-plugin/plugin.json",
                str(exc),
            )
        ], {}
    if not isinstance(manifest, dict):
        return [
            GateIssue(
                "PLUGIN_MANIFEST_INVALID",
                ".codex-plugin/plugin.json",
                "manifest root must be an object",
            )
        ], {}

    name = str(manifest.get("name") or "")
    if not PLUGIN_NAME_RE.fullmatch(name):
        issues.append(
            GateIssue(
                "PLUGIN_NAME_INVALID",
                ".codex-plugin/plugin.json",
                "name must use normalized lower-case hyphen-case",
            )
        )
    version = str(manifest.get("version") or "")
    if _semantic_base(version) is None:
        issues.append(
            GateIssue(
                "PLUGIN_VERSION_INVALID",
                ".codex-plugin/plugin.json",
                "version must be valid SemVer; build metadata is allowed",
            )
        )
    for field in ("description", "author", "skills", "mcpServers", "interface"):
        if field not in manifest:
            issues.append(
                GateIssue(
                    "PLUGIN_FIELD_MISSING",
                    ".codex-plugin/plugin.json",
                    f"required field is absent: {field}",
                )
            )

    skills_value = str(manifest.get("skills") or "")
    try:
        skills_root, _skills_relative = _repository_reference(
            root,
            skills_value,
            expect="dir",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        issues.append(
            GateIssue(
                "PLUGIN_SKILLS_MISSING",
                ".codex-plugin/plugin.json",
                f"invalid skills path {skills_value!r}: {exc}",
            )
        )
    else:
        skill_files = sorted(skills_root.glob("*/SKILL.md"))
        if not skill_files:
            issues.append(
                GateIssue(
                    "PLUGIN_SKILLS_EMPTY",
                    _relative(root, skills_root),
                    "no child skill contains SKILL.md",
                )
            )
        for skill_path in skill_files:
            skill_relative = _relative(root, skill_path)
            try:
                _repository_reference(
                    root,
                    skill_relative,
                    expect="file",
                    require_payload_file=True,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                issues.append(
                    GateIssue(
                        "SKILL_PATH_INVALID",
                        skill_relative,
                        str(exc),
                    )
                )
                continue
            frontmatter = _skill_frontmatter(skill_path)
            relative = _relative(root, skill_path)
            if not frontmatter:
                issues.append(
                    GateIssue(
                        "SKILL_FRONTMATTER_INVALID",
                        relative,
                        "SKILL.md must begin with closed YAML frontmatter",
                    )
                )
                continue
            if frontmatter.get("name") != skill_path.parent.name:
                issues.append(
                    GateIssue(
                        "SKILL_NAME_MISMATCH",
                        relative,
                        "frontmatter name must equal the skill directory name",
                    )
                )
            if not frontmatter.get("description"):
                issues.append(
                    GateIssue(
                        "SKILL_DESCRIPTION_MISSING",
                        relative,
                        "frontmatter description must be non-empty",
                    )
                )
            for line_number, line in enumerate(
                skill_path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                stripped = line.strip()
                if (
                    "CLAUDE_PLUGIN_ROOT" not in stripped
                    or not stripped
                    or not _is_python_command(stripped.split(maxsplit=1)[0])
                ):
                    continue
                try:
                    tokens = _shell_tokens(stripped.rstrip("`").rstrip())
                except ValueError:
                    tokens = ()
                if not _python_bytecode_disabled(tokens):
                    issues.append(
                        GateIssue(
                            "SKILL_PYTHON_BYTECODE_ENABLED",
                            relative,
                            (
                                "installed-cache Python examples that use "
                                "CLAUDE_PLUGIN_ROOT must pass -B"
                            ),
                            line=line_number,
                        )
                    )

    mcp_value = str(manifest.get("mcpServers") or "")
    try:
        mcp_path, mcp_relative = _repository_reference(
            root,
            mcp_value,
            expect="file",
            require_payload_file=True,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        issues.append(
            GateIssue(
                "PLUGIN_MCP_MISSING",
                ".codex-plugin/plugin.json",
                f"invalid MCP manifest path {mcp_value!r}: {exc}",
            )
        )
    else:
        try:
            mcp = _load_json(mcp_path)
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(
                GateIssue(
                    "PLUGIN_MCP_INVALID",
                    mcp_relative,
                    str(exc),
                )
            )
        else:
            if not isinstance(mcp, dict) or set(mcp) != {"mcpServers"}:
                issues.append(
                    GateIssue(
                        "PLUGIN_MCP_INVALID",
                        mcp_relative,
                        (
                            "MCP manifest root must contain exactly the "
                            "mcpServers object"
                        ),
                    )
                )
            servers = mcp.get("mcpServers") if isinstance(mcp, dict) else None
            if not isinstance(servers, dict) or not servers:
                issues.append(
                    GateIssue(
                        "PLUGIN_MCP_INVALID",
                        mcp_relative,
                        "mcpServers must be a non-empty object",
                    )
                )
            else:
                if set(servers) != {EXPECTED_MCP_SERVER_NAME}:
                    issues.append(
                        GateIssue(
                            "PLUGIN_MCP_SERVER_INVALID",
                            mcp_relative,
                            (
                                "mcpServers must contain exactly "
                                f"{EXPECTED_MCP_SERVER_NAME!r}"
                            ),
                        )
                    )
                for server_name, server in servers.items():
                    if not isinstance(server, dict):
                        issues.append(
                            GateIssue(
                                "PLUGIN_MCP_SERVER_INVALID",
                                mcp_relative,
                                f"{server_name!r} must be an object",
                            )
                        )
                        continue
                    if set(server) != {"command", "args", "cwd"}:
                        issues.append(
                            GateIssue(
                                "PLUGIN_MCP_SERVER_INVALID",
                                mcp_relative,
                                (
                                    f"{server_name!r} must contain exactly "
                                    "command, args, and cwd; env, disabled, and "
                                    "other execution overrides are forbidden"
                                ),
                            )
                        )
                    command = server.get("command")
                    if not isinstance(command, str) or not command.strip():
                        issues.append(
                            GateIssue(
                                "PLUGIN_MCP_SERVER_INVALID",
                                mcp_relative,
                                f"{server_name!r} has no string command",
                            )
                        )
                        continue
                    if _looks_like_repository_path(
                        root,
                        command,
                        command=True,
                    ):
                        try:
                            _repository_path_argument(root, command)
                        except (OSError, RuntimeError, ValueError) as exc:
                            issues.append(
                                GateIssue(
                                    "PLUGIN_MCP_COMMAND_INVALID",
                                    mcp_relative,
                                    (
                                        f"{server_name!r} uses an invalid command "
                                        f"path {command!r}: {exc}"
                                    ),
                                )
                            )
                    cwd_value = str(server.get("cwd") or ".")
                    try:
                        _cwd, cwd_relative = _repository_reference(
                            root,
                            cwd_value,
                            expect="dir",
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        issues.append(
                            GateIssue(
                                "PLUGIN_MCP_CWD_INVALID",
                                mcp_relative,
                                f"{server_name!r} has invalid cwd: {exc}",
                            )
                        )
                        continue
                    arguments = server.get("args") or []
                    if not isinstance(arguments, list) or not all(
                        isinstance(argument, str) for argument in arguments
                    ):
                        issues.append(
                            GateIssue(
                                "PLUGIN_MCP_ARGS_INVALID",
                                mcp_relative,
                                f"{server_name!r} args must be a list of strings",
                            )
                        )
                        continue
                    if (
                        server_name != EXPECTED_MCP_SERVER_NAME
                        or str(server.get("cwd") or ".") != EXPECTED_MCP_CWD
                        or tuple(arguments) != EXPECTED_MCP_ARGUMENTS
                    ):
                        issues.append(
                            GateIssue(
                                "PLUGIN_MCP_ENTRYPOINT_INVALID",
                                mcp_relative,
                                (
                                    "the packaged MCP entrypoint must use cwd "
                                    f"{EXPECTED_MCP_CWD!r} and arguments "
                                    f"{list(EXPECTED_MCP_ARGUMENTS)!r}; inline "
                                    "-c, module -m, version-only, and wrapper "
                                    "targets are not executable plugin servers"
                                ),
                            )
                        )
                    if (
                        not _is_python_command(command)
                        or PureWindowsPath(command).name != command
                    ):
                        issues.append(
                            GateIssue(
                                "PLUGIN_MCP_COMMAND_INVALID",
                                mcp_relative,
                                (
                                    f"{server_name!r} must invoke the Python "
                                    "interpreter directly; wrappers such as env "
                                    "or a shell bypass the verified -B entrypoint"
                                ),
                            )
                        )
                    elif not _python_bytecode_disabled((command, *arguments)):
                        issues.append(
                            GateIssue(
                                "PLUGIN_PYTHON_BYTECODE_ENABLED",
                                mcp_relative,
                                (
                                    f"{server_name!r} must pass -B as the first "
                                    "Python interpreter argument so the installed "
                                    "cache cannot create executable bytecode "
                                    "outside the verified payload"
                                ),
                            )
                        )
                    for argument in arguments:
                        candidate = str(argument)
                        if (
                            _has_option_assignment(candidate)
                            or _looks_like_repository_path(
                                root,
                                candidate,
                                base=cwd_relative,
                            )
                        ):
                            try:
                                _repository_path_argument(
                                    root,
                                    candidate,
                                    base=cwd_relative,
                                )
                            except (OSError, RuntimeError, ValueError) as exc:
                                issues.append(
                                    GateIssue(
                                        "PLUGIN_MCP_TARGET_MISSING",
                                        mcp_relative,
                                        (
                                            f"{server_name!r} references invalid "
                                            f"path {candidate!r}: {exc}"
                                        ),
                                    )
                                )
    return issues, manifest


def _validate_hooks(root: Path) -> list[GateIssue]:
    try:
        hook_tracked = "hooks/hooks.json" in tracked_files(root)
    except RuntimeError as exc:
        return [
            GateIssue(
                "HOOK_MANIFEST_INVALID",
                "hooks/hooks.json",
                str(exc),
            )
        ]
    if not hook_tracked:
        return [
            GateIssue(
                "HOOK_MANIFEST_INVALID",
                "hooks/hooks.json",
                "hook manifest must be tracked in the release payload",
            )
        ]
    try:
        path, _relative_path = _repository_reference(
            root,
            "hooks/hooks.json",
            expect="file",
            require_payload_file=True,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return [
            GateIssue(
                "HOOK_MANIFEST_INVALID",
                "hooks/hooks.json",
                str(exc),
            )
        ]
    try:
        payload = _load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return [GateIssue("HOOK_MANIFEST_INVALID", "hooks/hooks.json", str(exc))]
    issues: list[GateIssue] = []
    if not isinstance(payload, dict) or set(payload) != {"hooks"}:
        return [
            GateIssue(
                "HOOK_MANIFEST_INVALID",
                "hooks/hooks.json",
                "manifest root must contain only the hooks object",
            )
        ]
    hooks = payload["hooks"]
    if not isinstance(hooks, dict):
        return [
            GateIssue(
                "HOOK_MANIFEST_INVALID",
                "hooks/hooks.json",
                "hooks must be an object",
            )
        ]
    expected_events = set(EXPECTED_HOOK_ARGUMENTS)
    if set(hooks) != expected_events:
        issues.append(
            GateIssue(
                "HOOK_EVENT_INVALID",
                "hooks/hooks.json",
                (
                    "hooks must contain exactly the lifecycle events "
                    f"{sorted(expected_events)!r}"
                ),
            )
        )
    for event in EXPECTED_HOOK_ARGUMENTS:
        matchers = hooks.get(event)
        if not isinstance(matchers, list) or len(matchers) != 1:
            issues.append(
                GateIssue(
                    "HOOK_EVENT_INVALID",
                    "hooks/hooks.json",
                    f"{event!r} must contain exactly one matcher wrapper",
                )
            )
            continue
        matcher = matchers[0]
        if not isinstance(matcher, dict):
            issues.append(
                GateIssue(
                    "HOOK_MATCHER_INVALID",
                    "hooks/hooks.json",
                    f"{event!r} matcher wrapper must be an object",
                )
            )
            continue
        session_lifecycle_event = event == "SessionStart"
        expected_matcher_keys = (
            {"matcher", "hooks"} if session_lifecycle_event else {"hooks"}
        )
        if set(matcher) != expected_matcher_keys:
            issues.append(
                GateIssue(
                    "HOOK_MATCHER_INVALID",
                    "hooks/hooks.json",
                    (
                        f"{event!r} matcher wrapper must contain exactly "
                        f"{sorted(expected_matcher_keys)!r}"
                    ),
                )
            )
        if session_lifecycle_event and matcher.get("matcher") != "*":
            issues.append(
                GateIssue(
                    "HOOK_MATCHER_INVALID",
                    "hooks/hooks.json",
                    f"{event} matcher must be exactly '*'",
                )
            )
        commands = matcher.get("hooks")
        if not isinstance(commands, list) or len(commands) != 1:
            issues.append(
                GateIssue(
                    "HOOK_MATCHER_INVALID",
                    "hooks/hooks.json",
                    f"{event!r} matcher must contain exactly one command hook",
                )
            )
            continue
        hook = commands[0]
        if not isinstance(hook, dict):
            issues.append(
                GateIssue(
                    "HOOK_COMMAND_INVALID",
                    "hooks/hooks.json",
                    f"{event!r} command hook must be an object",
                )
            )
            continue
        if set(hook) != {"type", "command", "timeout"}:
            issues.append(
                GateIssue(
                    "HOOK_COMMAND_INVALID",
                    "hooks/hooks.json",
                    (
                        f"{event!r} command hook must contain exactly type, "
                        "command, and timeout; async and extra fields are "
                        "forbidden"
                    ),
                )
            )
        if hook.get("type") != "command":
            issues.append(
                GateIssue(
                    "HOOK_COMMAND_INVALID",
                    "hooks/hooks.json",
                    f"{event!r} hook type must be exactly 'command'",
                )
            )
        timeout = hook.get("timeout")
        if (
            type(timeout) is not int
            or timeout != EXPECTED_HOOK_TIMEOUTS[event]
        ):
            issues.append(
                GateIssue(
                    "HOOK_TIMEOUT_INVALID",
                    "hooks/hooks.json",
                    (
                        f"{event!r} timeout must be exactly "
                        f"{EXPECTED_HOOK_TIMEOUTS[event]}"
                    ),
                )
            )
        command = str(hook.get("command") or "")
        control_character = next(
            (
                character
                for character in command
                if character in {"\x00", "\r", "\n"}
            ),
            None,
        )
        if control_character is not None:
            issues.append(
                GateIssue(
                    "HOOK_COMMAND_INVALID",
                    "hooks/hooks.json",
                    (
                        f"{event!r} command contains forbidden control "
                        f"character {control_character!r}; hook commands "
                        "must be a single shell statement"
                    ),
                )
            )
        references = list(PLUGIN_ROOT_REFERENCE_RE.finditer(command))
        if len(references) != 1:
            issues.append(
                GateIssue(
                    "HOOK_TARGET_UNBOUND",
                    "hooks/hooks.json",
                    (
                        f"{event!r} command must contain exactly one "
                        "CLAUDE_PLUGIN_ROOT target"
                    ),
                )
            )
        for matched in references:
            relative = matched.group("path").replace("\\", "/")
            try:
                _repository_reference(
                    root,
                    relative,
                    expect="file",
                    require_payload_file=True,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                issues.append(
                    GateIssue(
                        "HOOK_TARGET_MISSING",
                        "hooks/hooks.json",
                        (
                            f"{event!r} references invalid file "
                            f"{relative!r}: {exc}"
                        ),
                    )
                )
        unsafe_fragment = SHELL_UNSAFE_PATH_RE.search(command)
        if unsafe_fragment is not None:
            issues.append(
                GateIssue(
                    "HOOK_ARGUMENT_PATH_INVALID",
                    "hooks/hooks.json",
                    (
                        f"{event!r} command contains an absolute, "
                        "home-relative, or parent-traversing path "
                        f"fragment: {unsafe_fragment.group(0).strip()!r}"
                    ),
                )
            )
        residual_command = command.replace("${CLAUDE_PLUGIN_ROOT}", "")
        dynamic_fragment = SHELL_DYNAMIC_PATH_RE.search(residual_command)
        if dynamic_fragment is not None:
            issues.append(
                GateIssue(
                    "HOOK_ARGUMENT_PATH_INVALID",
                    "hooks/hooks.json",
                    (
                        f"{event!r} command contains a shell metacharacter or "
                        "expansion outside the exact CLAUDE_PLUGIN_ROOT "
                        f"reference: {dynamic_fragment.group(0)!r}"
                    ),
                )
            )
        try:
            tokens = _shell_tokens(command)
        except ValueError as exc:
            issues.append(
                GateIssue(
                    "HOOK_COMMAND_INVALID",
                    "hooks/hooks.json",
                    f"{event!r} command is malformed: {exc}",
                )
            )
            tokens = ()
        expected_arguments = EXPECTED_HOOK_ARGUMENTS[event]
        if tokens and (
            not _is_python_command(tokens[0])
            or tuple(
                token.replace("\\", "/")
                for token in tokens[1:]
            )
            != expected_arguments
        ):
            issues.append(
                GateIssue(
                    "HOOK_COMMAND_INVALID",
                    "hooks/hooks.json",
                    (
                        f"{event!r} hook must invoke the packaged "
                        "plot_progression_gate.py directly with the exact "
                        "interpreter options and event flag; environment "
                        "assignments, wrappers, -c, -m, -V, and trailing "
                        "arguments are not valid entrypoints"
                    ),
                )
            )
        if (
            tokens
            and _is_python_command(tokens[0])
            and not _python_bytecode_disabled(tokens)
        ):
            issues.append(
                GateIssue(
                    "HOOK_PYTHON_BYTECODE_ENABLED",
                    "hooks/hooks.json",
                    (
                        f"{event!r} Python hook must pass -B as the first "
                        "interpreter argument so the installed cache remains "
                        "bytecode-free"
                    ),
                )
            )
        for index, token in enumerate(tokens):
            candidate = _path_argument_value(token)
            if (
                not candidate
                or "${CLAUDE_PLUGIN_ROOT}" in candidate
                or (
                    not _has_option_assignment(token)
                    and not _looks_like_repository_path(
                        root,
                        candidate,
                        command=index == 0,
                        fail_closed_separators=True,
                    )
                )
            ):
                continue
            try:
                _repository_path_argument(root, candidate)
            except (OSError, RuntimeError, ValueError) as exc:
                issues.append(
                    GateIssue(
                        "HOOK_ARGUMENT_PATH_INVALID",
                        "hooks/hooks.json",
                        (
                            f"{event!r} command references invalid path "
                            f"argument {candidate!r}: {exc}"
                        ),
                    )
                )
    return issues


def _json_pointer_exists(document: Any, fragment: str) -> bool:
    if not fragment or fragment == "#":
        return True
    if not fragment.startswith("#/"):
        return False
    current = document
    for token in fragment[2:].split("/"):
        if re.search(r"~(?:[^01]|$)", token):
            return False
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and token in current:
            current = current[token]
        elif (
            isinstance(current, list)
            and re.fullmatch(r"(?:0|[1-9]\d*)", token)
            and int(token) < len(current)
        ):
            current = current[int(token)]
        else:
            return False
    return True


def _walk_json(value: Any, location: str = "$") -> Iterable[tuple[str, Any]]:
    yield location, value
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk_json(child, f"{location}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json(child, f"{location}/{index}")


def _validate_schemas(root: Path) -> list[GateIssue]:
    schema_root = root / "schemas"
    paths = sorted(schema_root.rglob("*.json"))
    issues: list[GateIssue] = []
    documents: dict[Path, Any] = {}
    ids: dict[str, Path] = {}
    for path in paths:
        relative = _relative(root, path)
        try:
            document = _load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(GateIssue("SCHEMA_JSON_INVALID", relative, str(exc)))
            continue
        documents[path.resolve()] = document
        schema_id = document.get("$id") if isinstance(document, dict) else None
        if not isinstance(schema_id, str) or not schema_id:
            issues.append(
                GateIssue("SCHEMA_ID_MISSING", relative, "schema needs a stable $id")
            )
        elif schema_id in ids:
            issues.append(
                GateIssue(
                    "SCHEMA_ID_DUPLICATE",
                    relative,
                    f"$id is also used by {_relative(root, ids[schema_id])}",
                )
            )
        else:
            ids[schema_id] = path.resolve()
        if not isinstance(document, dict) or document.get("$schema") != (
            "https://json-schema.org/draft/2020-12/schema"
        ):
            issues.append(
                GateIssue(
                    "SCHEMA_DRAFT_INVALID",
                    relative,
                    "schema must declare JSON Schema draft 2020-12",
                )
            )

    for path, document in documents.items():
        relative = _relative(root, path)
        for location, value in _walk_json(document):
            if not isinstance(value, Mapping) or not isinstance(
                value.get("$ref"), str
            ):
                continue
            reference = str(value["$ref"])
            if "\\" in reference:
                issues.append(
                    GateIssue(
                        "SCHEMA_REF_INVALID",
                        relative,
                        (
                            f"{location} uses a non-portable backslash "
                            f"schema reference: {reference}"
                        ),
                    )
                )
                continue
            base, fragment = urllib.parse.urldefrag(reference)
            if base.startswith(("http://", "https://")):
                target = ids.get(base)
                if target is None:
                    issues.append(
                        GateIssue(
                            "SCHEMA_REF_UNRESOLVED",
                            relative,
                            f"{location} references unknown $id: {reference}",
                        )
                    )
                    continue
            else:
                target = (
                    (path.parent / urllib.parse.unquote(base)).resolve()
                    if base
                    else path
                )
                if target not in documents:
                    issues.append(
                        GateIssue(
                            "SCHEMA_REF_MISSING",
                            relative,
                            f"{location} references missing file: {reference}",
                        )
                    )
                    continue
            pointer = (
                f"#{urllib.parse.unquote(fragment)}"
                if fragment
                else "#"
            )
            if not _json_pointer_exists(documents[target], pointer):
                issues.append(
                    GateIssue(
                        "SCHEMA_REF_FRAGMENT_MISSING",
                        relative,
                        f"{location} references missing fragment: {reference}",
                    )
                )
    return issues


def _git_manifest_version(
    root: Path,
    revision: str,
) -> tuple[bool, str | None]:
    result = _run(
        ["git", "show", f"{revision}:.codex-plugin/plugin.json"],
        cwd=root,
        check=False,
    )
    if result.returncode != 0:
        return False, None
    try:
        manifest = json.loads(
            result.stdout,
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
    except json.JSONDecodeError:
        return True, None
    if not isinstance(manifest, Mapping):
        return True, None
    return True, str(manifest.get("version") or "")


def _changed_allowed_payload_paths(
    root: Path,
    *,
    staged_against: str | None = None,
    left: str | None = None,
    right: str | None = None,
) -> tuple[str, ...]:
    if staged_against is not None:
        arguments = [
            "git",
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACDMRTUXB",
            "-z",
            staged_against,
            "--",
        ]
    elif left is not None and right is not None:
        arguments = [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACDMRTUXB",
            "-z",
            left,
            right,
            "--",
        ]
    else:
        raise ValueError("a staged baseline or two revisions are required")
    result = _run(arguments, cwd=root, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            detail or "Git could not compare release payload revisions"
        )
    return tuple(
        sorted(
            {
                relative.replace("\\", "/")
                for relative in result.stdout.split("\0")
                if relative
                and not _is_noise(relative)
                and _is_allowed_payload_file(relative)
            }
        )
    )


def _cachebuster_stale_issue(
    *,
    current_version: str | None,
    baseline_version: str | None,
    changed_paths: Sequence[str],
    comparison: str,
) -> GateIssue | None:
    current_base = _semantic_base(str(current_version or ""))
    baseline_base = _semantic_base(str(baseline_version or ""))
    if current_base is None or current_base != baseline_base or not changed_paths:
        return None
    current_token = _cachebuster_token(str(current_version or ""))
    baseline_token = _cachebuster_token(str(baseline_version or ""))
    if current_token != baseline_token:
        return None
    sample = ", ".join(changed_paths[:5])
    if len(changed_paths) > 5:
        sample += f", ... (+{len(changed_paths) - 5})"
    return GateIssue(
        "VERSION_CACHEBUSTER_STALE",
        ".codex-plugin/plugin.json",
        (
            f"{comparison} changes {len(changed_paths)} allowed payload "
            f"file(s) at semantic base {current_base!r}, but the cachebuster "
            f"token remains {current_token!r}; regenerate the cachebuster "
            f"after the payload is final. Changed paths: {sample}"
        ),
    )


def _validate_versions(
    root: Path,
    manifest: Mapping[str, Any],
) -> list[GateIssue]:
    issues: list[GateIssue] = []
    manifest_version = str(manifest.get("version") or "")
    base = _semantic_base(manifest_version)
    if base is None:
        return issues
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    matched = CHANGELOG_VERSION_RE.search(changelog)
    latest = matched.group(1) if matched else None
    constants = {
        "scripts/plot_state.py": _python_string_constant(
            root / "scripts" / "plot_state.py", "PLUGIN_VERSION"
        ),
        "scripts/plot_rag_mcp.py": _python_string_constant(
            root / "scripts" / "plot_rag_mcp.py", "SERVER_VERSION"
        ),
    }
    if latest != base:
        issues.append(
            GateIssue(
                "VERSION_CHANGELOG_MISMATCH",
                "CHANGELOG.md",
                f"latest release {latest!r} does not match manifest base {base!r}",
            )
        )
    for path, value in constants.items():
        if value != base:
            issues.append(
                GateIssue(
                    "VERSION_RUNTIME_MISMATCH",
                    path,
                    f"runtime version {value!r} does not match manifest base {base!r}",
                )
            )
    state_rag_user_agent = _python_string_constant(
        root / "scripts" / "state_rag.py",
        "_REMOTE_USER_AGENT",
    )
    expected_state_rag_user_agent = f"plot-rag-gate/{base} state-rag"
    if state_rag_user_agent != expected_state_rag_user_agent:
        issues.append(
            GateIssue(
                "VERSION_RUNTIME_MISMATCH",
                "scripts/state_rag.py",
                (
                    f"state-rag User-Agent {state_rag_user_agent!r} does not "
                    f"match {expected_state_rag_user_agent!r}"
                ),
            )
        )
    readme = (root / "README.md").read_text(encoding="utf-8")
    if f"`v{base}`" not in readme[:1000]:
        issues.append(
            GateIssue(
                "VERSION_README_MISMATCH",
                "README.md",
                f"README introduction does not identify current v{base}",
            )
        )
    head_manifest_available, head_version = _git_manifest_version(root, "HEAD")
    head_base = _semantic_base(str(head_version or ""))
    cachebuster_base = _cachebuster_base(manifest_version)
    cachebuster_token = _cachebuster_token(manifest_version)
    if cachebuster_base != base:
        issues.append(
            GateIssue(
                "VERSION_CACHEBUSTER_MISSING",
                ".codex-plugin/plugin.json",
                (
                    "release manifest must use "
                    f"{base}+codex.<cachebuster> build metadata"
                ),
            )
        )
    elif (
        not head_manifest_available or head_base == base
    ) and not _is_release_cachebuster(cachebuster_token):
        issues.append(
            GateIssue(
                "VERSION_CACHEBUSTER_NOT_RELEASE",
                ".codex-plugin/plugin.json",
                (
                    "a committed or standalone release payload must use the "
                    "official 14-digit UTC cachebuster instead of a dev token"
                ),
            )
        )
    staged_paths: tuple[str, ...] | None = None
    if head_manifest_available:
        try:
            staged_paths = _changed_allowed_payload_paths(
                root,
                staged_against="HEAD",
            )
        except RuntimeError as exc:
            issues.append(
                GateIssue(
                    "VERSION_CACHEBUSTER_CHECK_FAILED",
                    ".git",
                    (
                        "could not compare the staged payload with HEAD: "
                        f"{exc}"
                    ),
                )
            )
        else:
            staged_issue = _cachebuster_stale_issue(
                current_version=manifest_version,
                baseline_version=head_version,
                changed_paths=staged_paths,
                comparison="the staged index relative to HEAD",
            )
            if staged_issue is not None:
                issues.append(staged_issue)
    parent_manifest_available, parent_version = _git_manifest_version(
        root,
        "HEAD^1",
    )
    if (
        head_manifest_available
        and parent_manifest_available
        and staged_paths == ()
    ):
        try:
            committed_paths = _changed_allowed_payload_paths(
                root,
                left="HEAD^1",
                right="HEAD",
            )
        except RuntimeError as exc:
            issues.append(
                GateIssue(
                    "VERSION_CACHEBUSTER_CHECK_FAILED",
                    ".git",
                    (
                        "could not compare HEAD with its first parent: "
                        f"{exc}"
                    ),
                )
            )
        else:
            committed_issue = _cachebuster_stale_issue(
                current_version=head_version,
                baseline_version=parent_version,
                changed_paths=committed_paths,
                comparison="HEAD relative to its first parent",
            )
            if committed_issue is not None:
                issues.append(committed_issue)
    expected_tag = f"v{base}"
    if (
        os.environ.get("GITHUB_REF_TYPE") == "tag"
        and os.environ.get("GITHUB_REF_NAME") != expected_tag
    ):
        issues.append(
            GateIssue(
                "VERSION_TAG_MISMATCH",
                ".git",
                (
                    f"GitHub tag {os.environ.get('GITHUB_REF_NAME')!r} does "
                    f"not match manifest base {base!r}"
                ),
            )
        )
    head_commit_result = _run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=root,
        check=False,
    )
    expected_tag_result = _run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/tags/{expected_tag}^{{commit}}",
        ],
        cwd=root,
        check=False,
    )
    if (
        head_commit_result.returncode == 0
        and expected_tag_result.returncode == 0
        and head_commit_result.stdout.strip()
        != expected_tag_result.stdout.strip()
    ):
        issues.append(
            GateIssue(
                "VERSION_TAG_MISMATCH",
                ".git",
                (
                    f"existing tag {expected_tag!r} points to "
                    f"{expected_tag_result.stdout.strip()[:12]}, not HEAD "
                    f"{head_commit_result.stdout.strip()[:12]}"
                ),
            )
        )
    tag_result = (
        _run(
            ["git", "tag", "--points-at", "HEAD", "--list", "v*"],
            cwd=root,
            check=False,
        )
        if head_base == base
        else None
    )
    if tag_result is not None and tag_result.returncode == 0:
        semantic_tags = sorted(
            {
                tag.strip()
                for tag in tag_result.stdout.splitlines()
                if re.fullmatch(r"v\d+\.\d+\.\d+", tag.strip())
            }
        )
        for tag in semantic_tags:
            if tag != expected_tag:
                issues.append(
                    GateIssue(
                        "VERSION_TAG_MISMATCH",
                        ".git",
                        (
                            f"HEAD tag {tag!r} does not match manifest "
                            f"base {base!r}"
                        ),
                    )
                )
    return issues


def _strip_yaml_comment(line: str) -> str:
    single_quoted = False
    double_quoted = False
    escaped = False
    for index, character in enumerate(line):
        if character == "\\" and double_quoted and not escaped:
            escaped = True
            continue
        if character == "'" and not double_quoted:
            single_quoted = not single_quoted
        elif character == '"' and not single_quoted and not escaped:
            double_quoted = not double_quoted
        elif (
            character == "#"
            and not single_quoted
            and not double_quoted
            and (index == 0 or line[index - 1].isspace())
        ):
            return line[:index].rstrip()
        escaped = False
    return line.rstrip()


def _yaml_key_value(value: str) -> tuple[str, str] | None:
    matched = re.fullmatch(
        (
            r"(?P<key>(?:[A-Za-z0-9_.-]+|"
            r"'[A-Za-z0-9_.-]+'|"
            r'"[A-Za-z0-9_.-]+")) *:'
            r"(?:\s*(?P<value>.*))?"
        ),
        value,
    )
    if matched is None:
        return None
    return (
        _unquote_scalar(matched.group("key")),
        _unquote_scalar(matched.group("value") or ""),
    )


def _parse_ci_workflow(
    text: str,
) -> tuple[
    set[str],
    dict[str, list[str]],
    dict[str, str],
    dict[str, dict[str, Any]],
]:
    logical: list[tuple[int, int, str]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        leading = raw_line[: len(raw_line) - len(raw_line.lstrip())]
        if "\t" in leading:
            raise ValueError(
                "tabs are not allowed in workflow indentation at line "
                f"{line_number}"
            )
        uncommented = _strip_yaml_comment(raw_line)
        if not uncommented.strip():
            continue
        indent = len(uncommented) - len(uncommented.lstrip(" "))
        logical.append((line_number, indent, uncommented.strip()))

    triggers: set[str] = set()
    trigger_options: dict[str, list[str]] = {}
    permissions: dict[str, str] = {}
    jobs: dict[str, dict[str, Any]] = {}
    top_level_keys: set[str] = set()
    section = ""
    current_trigger = ""
    current_job_name = ""
    current_job: dict[str, Any] | None = None
    current_step: dict[str, Any] | None = None
    current_job_mapping = ""
    current_matrix_axis = ""
    current_step_mapping = ""
    in_steps = False
    index = 0
    while index < len(logical):
        line_number, indent, content = logical[index]
        protected = current_job_name in CI_REQUIRED_JOB_COMMANDS
        if indent == 0:
            parsed = _yaml_key_value(content)
            if parsed is None:
                raise ValueError(
                    f"unrecognized top-level mapping at line {line_number}"
                )
            key, value = parsed
            if key in top_level_keys:
                raise ValueError(
                    f"duplicate top-level key {key!r} at line {line_number}"
                )
            top_level_keys.add(key)
            section = key if not value else ""
            if key == "on" and value:
                if value.startswith("[") and value.endswith("]"):
                    triggers.update(
                        _unquote_scalar(item.strip())
                        for item in value[1:-1].split(",")
                        if item.strip()
                    )
                else:
                    triggers.add(_unquote_scalar(value))
            current_trigger = ""
            current_job_name = ""
            current_job = None
            current_step = None
            current_job_mapping = ""
            current_matrix_axis = ""
            current_step_mapping = ""
            in_steps = False
            index += 1
            continue
        if section == "on":
            if indent == 2:
                if content.startswith("-"):
                    current_trigger = _unquote_scalar(content[1:].strip())
                    if current_trigger:
                        triggers.add(current_trigger)
                else:
                    parsed = _yaml_key_value(content)
                    if parsed is None:
                        raise ValueError(
                            "unrecognized trigger mapping at line "
                            f"{line_number}"
                        )
                    current_trigger, value = parsed
                    triggers.add(current_trigger)
                    if value.strip().casefold() not in {"", "null", "~", "{}"}:
                        trigger_options.setdefault(
                            current_trigger,
                            [],
                        ).append(
                            f"line {line_number}: inline value {value!r}"
                        )
            elif indent > 2 and current_trigger in {"push", "pull_request"}:
                trigger_options.setdefault(current_trigger, []).append(
                    f"line {line_number}: nested option {content!r}"
                )
            index += 1
            continue
        if section == "permissions" and indent == 2:
            parsed = _yaml_key_value(content)
            if parsed is None:
                raise ValueError(
                    f"unrecognized permissions mapping at line {line_number}"
                )
            if parsed[0] in permissions:
                raise ValueError(
                    "duplicate permissions key "
                    f"{parsed[0]!r} at line {line_number}"
                )
            permissions[parsed[0]] = parsed[1]
            index += 1
            continue
        if section != "jobs":
            index += 1
            continue
        if indent == 2:
            parsed = _yaml_key_value(content)
            if parsed is None or parsed[1]:
                raise ValueError(
                    f"unrecognized job mapping at line {line_number}"
                )
            job_name = parsed[0]
            if job_name in jobs:
                raise ValueError(
                    f"duplicate job {job_name!r} at line {line_number}"
                )
            current_job = {"fields": {}, "steps": []}
            jobs[job_name] = current_job
            current_job_name = job_name
            current_step = None
            current_job_mapping = ""
            current_matrix_axis = ""
            current_step_mapping = ""
            in_steps = False
            index += 1
            continue
        if current_job is None:
            index += 1
            continue
        protected = current_job_name in CI_REQUIRED_JOB_COMMANDS
        if indent == 4:
            parsed = _yaml_key_value(content)
            if parsed is None:
                if protected:
                    raise ValueError(
                        "unrecognized protected job mapping at line "
                        f"{line_number}"
                    )
                index += 1
                continue
            key, value = parsed
            if key == "steps" and not value:
                in_steps = True
                current_step = None
                current_job_mapping = ""
                current_matrix_axis = ""
                current_step_mapping = ""
            else:
                if key in current_job["fields"]:
                    raise ValueError(
                        f"duplicate job key {key!r} at line {line_number}"
                    )
                if key in {
                    "strategy",
                    "env",
                    "permissions",
                    "defaults",
                } and not value:
                    current_job["fields"][key] = {}
                    current_job_mapping = key
                else:
                    current_job["fields"][key] = value
                    current_job_mapping = ""
                current_matrix_axis = ""
                current_step_mapping = ""
                current_step = None
                in_steps = False
            index += 1
            continue
        if not in_steps:
            if indent == 6 and current_job_mapping in {
                "strategy",
                "env",
                "permissions",
                "defaults",
            }:
                parsed = _yaml_key_value(content)
                if parsed is None:
                    if protected:
                        raise ValueError(
                            "unrecognized protected nested job mapping at "
                            f"line {line_number}"
                        )
                    index += 1
                    continue
                key, value = parsed
                target = current_job["fields"][current_job_mapping]
                if key in target:
                    raise ValueError(
                        "duplicate nested job key "
                        f"{key!r} at line {line_number}"
                    )
                if (
                    current_job_mapping == "strategy"
                    and key == "matrix"
                    and not value
                ):
                    target[key] = {}
                    current_job_mapping = "matrix"
                    current_matrix_axis = ""
                else:
                    target[key] = value
                index += 1
                continue
            if indent == 8 and current_job_mapping == "matrix":
                parsed = _yaml_key_value(content)
                if parsed is None or parsed[1]:
                    if protected:
                        raise ValueError(
                            "unrecognized protected matrix mapping at line "
                            f"{line_number}"
                        )
                    index += 1
                    continue
                current_matrix_axis = parsed[0]
                matrix = current_job["fields"]["strategy"]["matrix"]
                if current_matrix_axis in matrix:
                    raise ValueError(
                        "duplicate matrix axis "
                        f"{current_matrix_axis!r} at line {line_number}"
                    )
                matrix[current_matrix_axis] = []
                index += 1
                continue
            if (
                indent == 10
                and current_job_mapping == "matrix"
                and current_matrix_axis
                and content.startswith("-")
            ):
                value = _unquote_scalar(content[1:].strip())
                if not value:
                    raise ValueError(
                        f"empty matrix value at line {line_number}"
                    )
                current_job["fields"]["strategy"]["matrix"][
                    current_matrix_axis
                ].append(value)
                index += 1
                continue
            if protected:
                raise ValueError(
                    "unrecognized protected nested job structure at line "
                    f"{line_number}"
                )
            index += 1
            continue
        if indent == 6 and content.startswith("-"):
            current_step = {"with": {}}
            current_job["steps"].append(current_step)
            current_step_mapping = ""
            remainder = content[1:].strip()
            parsed = _yaml_key_value(remainder) if remainder else None
            if remainder and parsed is None and protected:
                raise ValueError(
                    "unrecognized protected step mapping at line "
                    f"{line_number}"
                )
            if parsed is not None:
                key, value = parsed
                if key in {"with", "env"} and not value:
                    current_step.setdefault(key, {})
                    current_step_mapping = key
                else:
                    current_step[key] = value
            index += 1
            continue
        if current_step is None:
            if protected:
                raise ValueError(
                    "protected job content appears outside a step at line "
                    f"{line_number}"
                )
            index += 1
            continue
        if indent == 8:
            parsed = _yaml_key_value(content)
            if parsed is None:
                if protected:
                    raise ValueError(
                        "unrecognized protected step mapping at line "
                        f"{line_number}"
                    )
                index += 1
                continue
            key, value = parsed
            if key in {"with", "env"} and not value:
                if key in current_step and current_step[key]:
                    raise ValueError(
                        f"duplicate step key {key!r} at line {line_number}"
                    )
                current_step.setdefault(key, {})
                current_step_mapping = key
                index += 1
                continue
            current_step_mapping = ""
            if key == "run" and value.startswith(("|", ">")):
                block_lines: list[str] = []
                scan = index + 1
                while scan < len(logical) and logical[scan][1] > indent:
                    block_lines.append(logical[scan][2])
                    scan += 1
                current_step[key] = "\n".join(block_lines)
                index = scan
                continue
            if key in current_step:
                raise ValueError(
                    f"duplicate step key {key!r} at line {line_number}"
                )
            current_step[key] = value
            index += 1
            continue
        if indent == 10 and current_step_mapping in {"with", "env"}:
            parsed = _yaml_key_value(content)
            if parsed is None:
                if protected:
                    raise ValueError(
                        "unrecognized protected nested step mapping at line "
                        f"{line_number}"
                    )
                index += 1
                continue
            target = current_step[current_step_mapping]
            if parsed[0] in target:
                raise ValueError(
                    "duplicate "
                    f"{current_step_mapping} key {parsed[0]!r} "
                    f"at line {line_number}"
                )
            target[parsed[0]] = parsed[1]
            index += 1
            continue
        if protected:
            raise ValueError(
                "unrecognized protected step structure at line "
                f"{line_number}"
            )
        index += 1
    return triggers, trigger_options, permissions, jobs


def _normalized_ci_run(value: Any) -> str:
    return " ".join(str(value or "").split())


def _expected_ci_job(job_name: str) -> dict[str, Any]:
    checkout_with = (
        {
            "fetch-depth": "0",
            "persist-credentials": "false",
        }
        if job_name == "release-gates"
        else {"persist-credentials": "false"}
    )
    setup_with = {
        "python-version": (
            "${{ matrix.python-version }}"
            if job_name == "test"
            else "3.13"
        )
    }
    common_steps: list[dict[str, Any]] = [
        {
            "with": checkout_with,
            "uses": (
                "actions/checkout@"
                "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
            ),
        },
        {
            "with": setup_with,
            "uses": (
                "actions/setup-python@"
                "ece7cb06caefa5fff74198d8649806c4678c61a1"
            ),
        },
    ]
    if job_name == "test":
        return {
            "fields": {
                "name": (
                    "Python ${{ matrix.python-version }} on ${{ matrix.os }}"
                ),
                "runs-on": "${{ matrix.os }}",
                "timeout-minutes": "35",
                "strategy": {
                    "fail-fast": "false",
                    "matrix": {
                        "os": ["ubuntu-latest", "windows-latest"],
                        "python-version": ["3.10", "3.13"],
                    },
                },
            },
            "steps": [
                *common_steps,
                {
                    "with": {},
                    "name": "Run unit tests",
                    "env": {
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONWARNINGS": "error::ResourceWarning",
                    },
                    "run": CI_REQUIRED_JOB_COMMANDS["test"][0],
                },
                {
                    "with": {},
                    "name": "Smoke the extracted plugin payload",
                    "run": CI_REQUIRED_JOB_COMMANDS["test"][1],
                },
            ],
        }
    if job_name == "release-gates":
        return {
            "fields": {
                "name": "Portable plugin release gates",
                "runs-on": "ubuntu-latest",
                "timeout-minutes": "10",
                "needs": "test",
            },
            "steps": [
                *common_steps,
                {
                    "with": {},
                    "name": (
                        "Validate plugin, skill, schema, version, and CI "
                        "contracts"
                    ),
                    "run": CI_REQUIRED_JOB_COMMANDS["release-gates"][0],
                },
                {
                    "with": {},
                    "name": (
                        "Scan tracked source and reachable Git history for "
                        "secrets"
                    ),
                    "run": CI_REQUIRED_JOB_COMMANDS["release-gates"][1],
                },
                {
                    "with": {},
                    "name": "Verify deterministic package round-trip",
                    "run": CI_REQUIRED_JOB_COMMANDS["release-gates"][2],
                },
            ],
        }
    raise KeyError(job_name)


def _normalized_ci_job(job: Mapping[str, Any]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for raw_step in job.get("steps", []):
        step = dict(raw_step)
        if "run" in step:
            step["run"] = _normalized_ci_run(step["run"])
        steps.append(step)
    return {
        "fields": dict(job.get("fields", {})),
        "steps": steps,
    }


def _validate_ci(root: Path) -> list[GateIssue]:
    path = root / ".github" / "workflows" / "ci.yml"
    if not path.is_file():
        return [
            GateIssue(
                "CI_WORKFLOW_MISSING",
                ".github/workflows/ci.yml",
                "portable release gates require the CI workflow",
            )
        ]
    text = path.read_text(encoding="utf-8")
    try:
        triggers, trigger_options, permissions, jobs = _parse_ci_workflow(text)
    except ValueError as exc:
        return [
            GateIssue(
                "CI_WORKFLOW_INVALID",
                ".github/workflows/ci.yml",
                str(exc),
            )
        ]
    issues: list[GateIssue] = []
    missing_triggers = sorted(CI_REQUIRED_TRIGGERS.difference(triggers))
    if missing_triggers:
        issues.append(
            GateIssue(
                "CI_TRIGGER_MISSING",
                ".github/workflows/ci.yml",
                (
                    "workflow must enable push, pull_request, and "
                    "workflow_dispatch; missing "
                    f"{missing_triggers}"
                ),
            )
        )
    for trigger in ("push", "pull_request"):
        options = trigger_options.get(trigger, [])
        if options:
            issues.append(
                GateIssue(
                    "CI_TRIGGER_FILTERED",
                    ".github/workflows/ci.yml",
                    (
                        f"workflow trigger {trigger!r} must be unfiltered; "
                        f"found {', '.join(options)}"
                    ),
                )
            )
    if permissions != {"contents": "read"}:
        issues.append(
            GateIssue(
                "CI_GATE_MISSING",
                ".github/workflows/ci.yml",
                (
                    "top-level permissions must contain exactly "
                    "contents: read and no additional scopes"
                ),
            )
        )
    for job_name, commands in CI_REQUIRED_JOB_COMMANDS.items():
        job = jobs.get(job_name)
        if job is None:
            issues.append(
                GateIssue(
                    "CI_GATE_MISSING",
                    ".github/workflows/ci.yml",
                    f"required job is absent: {job_name}",
                )
            )
            continue
        if "permissions" in job["fields"]:
            issues.append(
                GateIssue(
                    "CI_PERMISSION_ESCALATION",
                    ".github/workflows/ci.yml",
                    (
                        f"required job {job_name!r} must inherit the exact "
                        "top-level read-only permissions"
                    ),
                )
            )
        if "defaults" in job["fields"]:
            issues.append(
                GateIssue(
                    "CI_GATE_EXECUTION_OVERRIDE",
                    ".github/workflows/ci.yml",
                    (
                        f"required job {job_name!r} must not override run "
                        "defaults or working-directory"
                    ),
                )
            )
        if str(job["fields"].get("if") or "").strip():
            issues.append(
                GateIssue(
                    "CI_GATE_CONDITIONAL",
                    ".github/workflows/ci.yml",
                    f"required job {job_name!r} must not be conditional",
                )
            )
        if (
            str(job["fields"].get("continue-on-error") or "")
            .strip()
            .casefold()
            not in {"", "false"}
        ):
            issues.append(
                GateIssue(
                    "CI_GATE_NON_BLOCKING",
                    ".github/workflows/ci.yml",
                    (
                        f"required job {job_name!r} must block on failure; "
                        "job-level continue-on-error must be false"
                    ),
                )
            )
        steps = list(job["steps"])
        for command in commands:
            matches = [
                step
                for step in steps
                if _normalized_ci_run(step.get("run")) == command
            ]
            if len(matches) != 1:
                issues.append(
                    GateIssue(
                        "CI_GATE_MISSING",
                        ".github/workflows/ci.yml",
                        (
                            f"job {job_name!r} must contain exactly one active "
                            f"step with run: {command}"
                        ),
                    )
                )
                continue
            step = matches[0]
            if str(step.get("if") or "").strip():
                issues.append(
                    GateIssue(
                        "CI_GATE_CONDITIONAL",
                        ".github/workflows/ci.yml",
                        f"required command must not be conditional: {command}",
                    )
                )
            if str(step.get("continue-on-error") or "").strip().casefold() not in {
                "",
                "false",
            }:
                issues.append(
                    GateIssue(
                        "CI_GATE_NON_BLOCKING",
                        ".github/workflows/ci.yml",
                        f"required command must block on failure: {command}",
                    )
                )
            forbidden_step_keys = sorted(
                key
                for key in ("shell", "working-directory")
                if key in step
            )
            if forbidden_step_keys:
                issues.append(
                    GateIssue(
                        "CI_GATE_EXECUTION_OVERRIDE",
                        ".github/workflows/ci.yml",
                        (
                            f"required command must use the checked-out "
                            "workspace and default shell; forbidden keys "
                            f"{forbidden_step_keys}: {command}"
                        ),
                    )
                )

        expected_checkout_with = (
            {
                "fetch-depth": "0",
                "persist-credentials": "false",
            }
            if job_name == "release-gates"
            else {"persist-credentials": "false"}
        )
        checkout_steps = [
            step
            for step in steps
            if str(step.get("uses") or "").startswith("actions/checkout@")
        ]
        if (
            len(checkout_steps) != 1
            or str(checkout_steps[0].get("uses") or "")
            != (
                "actions/checkout@"
                "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
            )
            or dict(checkout_steps[0].get("with") or {})
            != expected_checkout_with
            or any(
                key in checkout_steps[0]
                for key in ("if", "continue-on-error", "shell", "working-directory")
            )
        ):
            issues.append(
                GateIssue(
                    "CI_GATE_MISSING",
                    ".github/workflows/ci.yml",
                    (
                        f"required job {job_name!r} must use exactly "
                        "actions/checkout@"
                        "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 "
                        "with "
                        f"{expected_checkout_with!r}, no ref/path/repository, "
                        "and no execution override"
                    ),
                )
            )
        expected_setup_with = {
            "python-version": (
                "${{ matrix.python-version }}"
                if job_name == "test"
                else "3.13"
            )
        }
        setup_steps = [
            step
            for step in steps
            if str(step.get("uses") or "").startswith("actions/setup-python@")
        ]
        if (
            len(setup_steps) != 1
            or str(setup_steps[0].get("uses") or "")
            != (
                "actions/setup-python@"
                "ece7cb06caefa5fff74198d8649806c4678c61a1"
            )
            or dict(setup_steps[0].get("with") or {}) != expected_setup_with
            or any(
                key in setup_steps[0]
                for key in ("if", "continue-on-error", "shell", "working-directory")
            )
        ):
            issues.append(
                GateIssue(
                    "CI_GATE_MISSING",
                    ".github/workflows/ci.yml",
                    (
                        f"required job {job_name!r} must use exactly "
                        "actions/setup-python@"
                        "ece7cb06caefa5fff74198d8649806c4678c61a1 "
                        "with "
                        f"{expected_setup_with!r}"
                    ),
                )
            )
        actual_contract = _normalized_ci_job(job)
        expected_contract = _expected_ci_job(job_name)
        if actual_contract != expected_contract:
            issues.append(
                GateIssue(
                    "CI_JOB_CONTRACT_INVALID",
                    ".github/workflows/ci.yml",
                    (
                        f"required job {job_name!r} must match the exact "
                        "runner, timeout, dependency, matrix, environment, "
                        "and ordered checkout/setup/run step contract; extra "
                        "run/uses steps and field overrides are forbidden"
                    ),
                )
            )
    return issues


def _validate_payload(root: Path) -> list[GateIssue]:
    issues: list[GateIssue] = []
    seen: set[tuple[str, str]] = set()
    try:
        entries = _tracked_paths(root)
    except RuntimeError as exc:
        return [
            GateIssue(
                "PACKAGE_GIT_INDEX_UNAVAILABLE",
                ".git",
                str(exc),
            )
        ]
    for entry in entries:
        relative = entry.path
        if _is_noise(relative):
            issues.append(
                GateIssue(
                    "PACKAGE_NOISE_TRACKED",
                    relative,
                    "tracked release payload contains cache, runtime state, or a secret file",
                )
            )
            continue
        if not _is_allowed_payload_file(relative):
            issues.append(
                GateIssue(
                    "PACKAGE_PATH_NOT_ALLOWED",
                    relative,
                    "tracked file is outside the explicit plugin payload allowlist",
                )
            )
            continue
        problem = _payload_path_problem(root, entry)
        if problem is not None:
            key = (problem.code, problem.path)
            if key not in seen:
                seen.add(key)
                issues.append(problem)
            continue
        try:
            staged_bytes = _git_blob_bytes(root, entry)
            worktree_bytes = _read_payload_bytes(
                root,
                relative,
                mode=entry.mode,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            issues.append(
                GateIssue(
                    "PACKAGE_FILE_UNREADABLE",
                    relative,
                    str(exc),
                )
            )
            continue
        if staged_bytes != worktree_bytes:
            issues.append(
                GateIssue(
                    "PACKAGE_INDEX_WORKTREE_MISMATCH",
                    relative,
                    (
                        "release payload bytes differ between the Git index "
                        "and worktree; stage the intended file first"
                    ),
                )
            )
            continue
        if _requires_lf(relative):
            if b"\r" in staged_bytes:
                issues.append(
                    GateIssue(
                        "PACKAGE_TEXT_EOL_MISMATCH",
                        relative,
                        (
                            "release text must use LF bytes so local installs "
                            "match Git/GitHub source archives"
                        ),
                    )
                )
    try:
        untracked = untracked_release_files(root)
    except RuntimeError as exc:
        issues.append(
            GateIssue(
                "PACKAGE_GIT_INDEX_UNAVAILABLE",
                ".git",
                str(exc),
            )
        )
        return issues
    for relative in untracked:
        issues.append(
            GateIssue(
                "PACKAGE_UNTRACKED_FILE",
                relative,
                "release-surface file is not tracked; stage it or exclude it before release",
            )
        )
    return issues


def validate_source(root: Path) -> list[GateIssue]:
    root = root.resolve()
    payload_issues = _validate_payload(root)
    blocking_payload_issues = [
        issue
        for issue in payload_issues
        if issue.code != "PACKAGE_UNTRACKED_FILE"
    ]
    if blocking_payload_issues:
        return sorted(
            payload_issues,
            key=lambda issue: (
                issue.code,
                issue.path,
                issue.line or 0,
                issue.message,
            ),
        )
    issues, manifest = _validate_manifest(root)
    issues.extend(_validate_hooks(root))
    issues.extend(_validate_schemas(root))
    issues.extend(_validate_ci(root))
    issues.extend(payload_issues)
    issues.extend(_validate_v15_payload_membership(root))
    issues.extend(_validate_advantage_v1_payload_membership(root))
    issues.extend(_validate_power_spec_payload_membership(root))
    issues.extend(_validate_source_manifest_contract(root))
    issues.extend(_validate_power_spec_contract(root))
    if manifest:
        issues.extend(_validate_versions(root, manifest))
    config_v3: Mapping[str, Any] | None = None
    for relative in ("templates/config.v2.json", "templates/config.v3.json"):
        path = root / relative
        try:
            template = _load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(GateIssue("TEMPLATE_JSON_INVALID", relative, str(exc)))
        else:
            if relative == "templates/config.v3.json":
                if isinstance(template, Mapping):
                    config_v3 = template
                else:
                    issues.append(
                        GateIssue(
                            "TEMPLATE_JSON_INVALID",
                            relative,
                            "template root must be an object",
                        )
                    )
    if config_v3 is not None:
        issues.extend(_validate_v15_contract(root, config_v3))
        issues.extend(_validate_advantage_v1_contract(root, config_v3))
    return sorted(
        issues,
        key=lambda issue: (issue.code, issue.path, issue.line or 0, issue.message),
    )


def _is_noise(relative: str) -> bool:
    path = Path(relative.replace("\\", "/"))
    parts = tuple(part.casefold() for part in path.parts)
    if any(part in NOISE_PARTS for part in parts):
        return True
    name = path.name.casefold()
    if name in NOISE_NAMES:
        return True
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    return name.endswith(NOISE_SUFFIXES)


def _is_allowed_payload_file(relative: str) -> bool:
    normalized = relative.replace("\\", "/")
    return (
        normalized in PAYLOAD_ALLOWED_ROOT_FILES
        or normalized in PAYLOAD_ALLOWED_BENCHMARK_FILES
        or any(
            normalized.startswith(prefix)
            for prefix in PAYLOAD_ALLOWED_PREFIXES
        )
    )


def _requires_lf(relative: str) -> bool:
    path = Path(relative.replace("\\", "/"))
    return (
        path.name in LF_TEXT_NAMES
        or path.suffix.casefold() in LF_TEXT_SUFFIXES
    )


def _tracked_paths(root: Path) -> tuple[TrackedPath, ...]:
    root = root.resolve()
    git = _run(
        [
            "git",
            "ls-files",
            "--stage",
            "-z",
        ],
        cwd=root,
        check=False,
    )
    if git.returncode != 0:
        detail = git.stderr.strip() or "Git index is unavailable"
        raise RuntimeError(f"PACKAGE_GIT_INDEX_UNAVAILABLE: {detail}")
    entries: list[TrackedPath] = []
    for record in (item for item in git.stdout.split("\0") if item):
        metadata, separator, raw_path = record.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise RuntimeError(
                "PACKAGE_GIT_INDEX_UNREADABLE: malformed git ls-files record"
            )
        mode, object_id, raw_stage = fields
        try:
            stage = int(raw_stage)
        except ValueError as exc:
            raise RuntimeError(
                "PACKAGE_GIT_INDEX_UNREADABLE: invalid index stage"
            ) from exc
        entries.append(
            TrackedPath(
                path=raw_path.replace("\\", "/"),
                mode=mode,
                stage=stage,
                object_id=object_id,
            )
        )
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                entry.path,
                entry.stage,
                entry.mode or "",
                entry.object_id or "",
            ),
        )
    )


def tracked_files(root: Path) -> tuple[str, ...]:
    return tuple(sorted({entry.path for entry in _tracked_paths(root)}))


def _git_blob_bytes(root: Path, entry: TrackedPath) -> bytes:
    if not entry.object_id:
        raise RuntimeError(
            f"PACKAGE_GIT_OBJECT_MISSING: {entry.path} has no staged object"
        )
    completed = subprocess.run(
        ["git", "cat-file", "blob", entry.object_id],
        cwd=root.resolve(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"PACKAGE_GIT_OBJECT_UNREADABLE: {entry.path}: {detail}"
        )
    return bytes(completed.stdout)


def _is_link_or_reparse(file_stat: os.stat_result) -> bool:
    attributes = int(getattr(file_stat, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(file_stat.st_mode) or bool(attributes & reparse_flag)


def _absolute_reparse_component(path: Path) -> Path | None:
    absolute = Path(os.path.abspath(path.expanduser()))
    anchor = Path(absolute.anchor) if absolute.anchor else Path()
    current = anchor
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        try:
            file_stat = os.lstat(current)
        except OSError:
            return None
        if _is_link_or_reparse(file_stat):
            return current
    return None


def _payload_path_problem(
    root: Path,
    entry: TrackedPath,
) -> GateIssue | None:
    root = root.resolve()
    normalized = entry.path.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return GateIssue(
            "PACKAGE_PATH_INVALID",
            normalized,
            "payload paths must be normalized relative paths without traversal",
        )
    portability_problem = _portable_path_problem(relative.parts)
    if portability_problem is not None:
        return GateIssue(
            "PACKAGE_PATH_INVALID",
            normalized,
            (
                "payload path is not portable across release platforms: "
                f"{portability_problem}"
            ),
        )
    if entry.stage != 0:
        return GateIssue(
            "PACKAGE_INDEX_CONFLICT",
            normalized,
            f"Git index stage {entry.stage} is unresolved",
        )
    if entry.mode == "120000":
        return GateIssue(
            "PACKAGE_LINK_TRACKED",
            normalized,
            "tracked symbolic links are not valid plugin payload files",
        )
    if entry.mode is not None and entry.mode != "100644":
        return GateIssue(
            "PACKAGE_GIT_MODE_UNSUPPORTED",
            normalized,
            (
                f"Git mode {entry.mode!r} is not a portable 100644 payload "
                "file"
            ),
        )

    current = root
    final_stat: os.stat_result | None = None
    try:
        root_streams = _unexpected_windows_directory_streams(root)
    except OSError as exc:
        return GateIssue(
            "PACKAGE_PATH_UNREADABLE",
            normalized,
            f"repository root streams cannot be inspected: {exc}",
        )
    if root_streams:
        return GateIssue(
            "PACKAGE_DIRECTORY_STREAM_UNSAFE",
            normalized,
            (
                "repository root contains alternate data streams: "
                f"{root_streams!r}"
            ),
        )
    for index, part in enumerate(relative.parts):
        current /= part
        try:
            final_stat = os.lstat(current)
        except FileNotFoundError:
            return GateIssue(
                "PACKAGE_TRACKED_FILE_MISSING",
                normalized,
                "tracked payload file is absent from the worktree",
            )
        except OSError as exc:
            return GateIssue(
                "PACKAGE_PATH_UNREADABLE",
                normalized,
                f"payload path cannot be inspected: {exc}",
            )
        if _is_link_or_reparse(final_stat):
            return GateIssue(
                "PACKAGE_LINK_TRACKED",
                normalized,
                (
                    "payload path contains a symbolic link, junction, or "
                    "other reparse point"
                ),
            )
        if index < len(relative.parts) - 1:
            if not stat.S_ISDIR(final_stat.st_mode):
                return GateIssue(
                    "PACKAGE_FILE_TYPE_UNSUPPORTED",
                    normalized,
                    "payload path parent is not a directory",
                )
            try:
                directory_streams = _unexpected_windows_directory_streams(
                    current
                )
            except OSError as exc:
                return GateIssue(
                    "PACKAGE_PATH_UNREADABLE",
                    normalized,
                    f"payload parent streams cannot be inspected: {exc}",
                )
            if directory_streams:
                return GateIssue(
                    "PACKAGE_DIRECTORY_STREAM_UNSAFE",
                    normalized,
                    (
                        "payload path parent contains alternate data streams: "
                        f"{directory_streams!r}"
                    ),
                )
    if final_stat is None or not stat.S_ISREG(final_stat.st_mode):
        return GateIssue(
            "PACKAGE_FILE_TYPE_UNSUPPORTED",
            normalized,
            "tracked payload entry is not a regular file",
        )
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        return GateIssue(
            "PACKAGE_PATH_UNREADABLE",
            normalized,
            f"payload path cannot be resolved: {exc}",
        )
    if resolved == root or root not in resolved.parents:
        return GateIssue(
            "PACKAGE_PATH_ESCAPE",
            normalized,
            f"payload path resolves outside repository root {root}",
        )
    return None


def _require_payload_path(
    root: Path,
    relative: str,
    *,
    mode: str | None = None,
) -> Path:
    entry = TrackedPath(
        path=relative.replace("\\", "/"),
        mode=mode,
        stage=0,
    )
    problem = _payload_path_problem(root, entry)
    if problem is not None:
        raise ValueError(problem.render())
    return root.resolve() / PurePosixPath(entry.path)


def _stat_snapshot(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        int(file_stat.st_dev),
        int(file_stat.st_ino),
        int(stat.S_IFMT(file_stat.st_mode)),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        int(file_stat.st_ctime_ns),
    )


def _cross_api_stat_snapshot(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        int(file_stat.st_dev),
        int(file_stat.st_ino),
        int(stat.S_IFMT(file_stat.st_mode)),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        int(
            getattr(
                file_stat,
                "st_birthtime_ns",
                file_stat.st_ctime_ns,
            )
        ),
    )


def _same_file_identity(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return (
        int(left.st_dev) == int(right.st_dev)
        and int(left.st_ino) == int(right.st_ino)
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
    )


def _windows_stream_names(path: Path) -> tuple[str, ...]:
    if os.name != "nt":
        return ()
    import ctypes
    from ctypes import wintypes

    class Win32FindStreamData(ctypes.Structure):
        _fields_ = [
            ("stream_size", ctypes.c_longlong),
            ("stream_name", wintypes.WCHAR * (260 + 36)),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel32.FindFirstStreamW
    find_first.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(Win32FindStreamData),
        wintypes.DWORD,
    ]
    find_first.restype = wintypes.HANDLE
    find_next = kernel32.FindNextStreamW
    find_next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Win32FindStreamData),
    ]
    find_next.restype = wintypes.BOOL
    find_close = kernel32.FindClose
    find_close.argtypes = [wintypes.HANDLE]
    find_close.restype = wintypes.BOOL

    data = Win32FindStreamData()
    handle = find_first(str(path), 0, ctypes.byref(data), 0)
    invalid_handle = wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        if error in {18, 38}:
            return ()
        raise OSError(error, ctypes.FormatError(error), str(path))
    names = [str(data.stream_name)]
    try:
        while True:
            if find_next(handle, ctypes.byref(data)):
                names.append(str(data.stream_name))
                continue
            error = ctypes.get_last_error()
            if error in {18, 38}:
                break
            raise OSError(error, ctypes.FormatError(error), str(path))
    finally:
        find_close(handle)
    return tuple(names)


def _unexpected_windows_directory_streams(path: Path) -> tuple[str, ...]:
    return tuple(
        stream
        for stream in _windows_stream_names(path)
        if stream != "::$DATA"
    )


def _require_default_windows_stream(path: Path, relative: str) -> None:
    streams = _windows_stream_names(path)
    if os.name == "nt" and streams != ("::$DATA",):
        raise ValueError(
            f"payload path contains alternate or invalid data streams: "
            f"{relative}: {streams!r}"
        )


def _read_payload_bytes(
    root: Path,
    relative: str,
    *,
    mode: str | None = None,
) -> bytes:
    path = _require_payload_path(root, relative, mode=mode)
    _require_default_windows_stream(path, relative)
    before = os.lstat(path)
    if _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"payload path is not a stable regular file: {relative}")
    flags = os.O_RDONLY
    flags |= int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        opened_before = os.fstat(descriptor)
        if (
            _is_link_or_reparse(opened_before)
            or not stat.S_ISREG(opened_before.st_mode)
            or not _same_file_identity(before, opened_before)
        ):
            raise ValueError(
                f"payload path changed identity before reading: {relative}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        opened_after = os.fstat(descriptor)
        if _stat_snapshot(opened_before) != _stat_snapshot(opened_after):
            raise ValueError(
                f"payload file changed while being read: {relative}"
            )
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    _require_payload_path(root, relative, mode=mode)
    _require_default_windows_stream(path, relative)
    after = os.lstat(path)
    if (
        _is_link_or_reparse(after)
        or not stat.S_ISREG(after.st_mode)
        or not _same_file_identity(opened_after, after)
        or _cross_api_stat_snapshot(opened_after)
        != _cross_api_stat_snapshot(after)
    ):
        raise ValueError(f"payload path changed after reading: {relative}")
    return data


def _payload_entry_bytes(root: Path, entry: TrackedPath) -> bytes:
    problem = _payload_path_problem(root, entry)
    if problem is not None:
        raise ValueError(problem.render())
    staged = _git_blob_bytes(root, entry)
    worktree = _read_payload_bytes(root, entry.path, mode=entry.mode)
    if staged != worktree:
        raise ValueError(
            GateIssue(
                "PACKAGE_INDEX_WORKTREE_MISMATCH",
                entry.path,
                (
                    "release payload bytes differ between the Git index and "
                    "worktree; stage the intended file first"
                ),
            ).render()
        )
    return staged


def untracked_release_files(root: Path) -> tuple[str, ...]:
    root = root.resolve()
    git = _run(
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=root,
        check=False,
    )
    if git.returncode != 0:
        detail = git.stderr.strip() or "Git untracked-file index is unavailable"
        raise RuntimeError(f"PACKAGE_GIT_INDEX_UNAVAILABLE: {detail}")
    return tuple(
        sorted(
            {
                relative.replace("\\", "/")
                for relative in git.stdout.split("\0")
                if relative
                and not _is_noise(relative)
            }
        )
    )


def payload_files(root: Path) -> tuple[str, ...]:
    root = root.resolve()
    selected: set[str] = set()
    for entry in _tracked_paths(root):
        relative = entry.path
        if _is_noise(relative) or not _is_allowed_payload_file(relative):
            continue
        _payload_entry_bytes(root, entry)
        selected.add(relative)
    return tuple(sorted(selected))


def payload_manifest(
    root: Path,
    files: Iterable[str] | None = None,
) -> dict[str, str]:
    root = root.resolve()
    selected = tuple(files) if files is not None else payload_files(root)
    try:
        entries = {
            entry.path: entry
            for entry in _tracked_paths(root)
            if entry.stage == 0
        }
    except RuntimeError:
        if files is None:
            raise
        entries = {}
    manifest: dict[str, str] = {}
    for relative in selected:
        entry = entries.get(relative)
        data = (
            _payload_entry_bytes(root, entry)
            if entry is not None
            else _read_payload_bytes(root, relative)
        )
        manifest[relative] = hashlib.sha256(data).hexdigest()
    return manifest


def _walk_install_tree(root: Path) -> tuple[set[str], set[str]]:
    regular: set[str] = set()
    unsafe: set[str] = set()
    resolved_root = root.resolve()
    stack = [resolved_root]
    while stack:
        directory = stack.pop()
        relative_directory = (
            "."
            if directory == resolved_root
            else directory.relative_to(resolved_root).as_posix()
        )
        try:
            directory_stat = os.lstat(directory)
            directory_resolved = directory.resolve(strict=True)
            directory_streams = _unexpected_windows_directory_streams(
                directory
            )
        except OSError:
            unsafe.add(relative_directory)
            continue
        if (
            _is_link_or_reparse(directory_stat)
            or not stat.S_ISDIR(directory_stat.st_mode)
            or (
                directory == resolved_root
                and directory_resolved != resolved_root
            )
            or (
                directory != resolved_root
                and resolved_root not in directory_resolved.parents
            )
            or bool(directory_streams)
        ):
            unsafe.add(relative_directory)
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError:
            relative = directory.relative_to(resolved_root).as_posix()
            if relative != ".":
                unsafe.add(relative)
            continue
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(resolved_root).as_posix()
            try:
                file_stat = os.lstat(path)
            except OSError:
                unsafe.add(relative)
                continue
            if _is_link_or_reparse(file_stat):
                unsafe.add(relative)
            elif stat.S_ISDIR(file_stat.st_mode):
                stack.append(path)
            elif stat.S_ISREG(file_stat.st_mode):
                if int(getattr(file_stat, "st_nlink", 1)) != 1:
                    unsafe.add(relative)
                else:
                    regular.add(relative)
            else:
                unsafe.add(relative)
    return regular, unsafe


def compare_install_tree(
    source: Path,
    installed: Path,
    *,
    files: Iterable[str] | None = None,
) -> TreeComparison:
    source = source.resolve()
    installed = installed.resolve()
    source_manifest = payload_manifest(source, files)
    installed_modes: dict[str, str | None] = {}
    missing: list[str] = []
    mismatched: list[str] = []
    for relative, expected in source_manifest.items():
        problem = _payload_path_problem(
            installed,
            TrackedPath(
                path=relative,
                mode=installed_modes.get(relative),
                stage=0,
            ),
        )
        if problem is not None:
            if problem.code == "PACKAGE_TRACKED_FILE_MISSING":
                missing.append(relative)
            else:
                mismatched.append(relative)
            continue
        target = installed / PurePosixPath(relative)
        if not target.is_file():
            missing.append(relative)
        else:
            try:
                actual = hashlib.sha256(
                    _read_payload_bytes(installed, relative)
                ).hexdigest()
            except (OSError, ValueError):
                mismatched.append(relative)
            else:
                if actual != expected:
                    mismatched.append(relative)
    installed_files, unsafe = _walk_install_tree(installed)
    for relative in source_manifest:
        if relative in unsafe:
            mismatched.append(relative)
            continue
        problem = _payload_path_problem(
            installed,
            TrackedPath(path=relative, mode=None, stage=0),
        )
        if problem is not None and relative not in missing:
            mismatched.append(relative)
    unexpected = sorted(
        installed_files.difference(source_manifest)
        | unsafe.difference(source_manifest)
    )
    return TreeComparison(
        missing=tuple(sorted(set(missing))),
        mismatched=tuple(sorted(set(mismatched))),
        unexpected=tuple(unexpected),
    )


def marketplace_source(
    marketplace_path: Path,
    plugin_name: str,
) -> tuple[Path, Mapping[str, Any]]:
    marketplace_path = marketplace_path.resolve()
    payload = _load_json(marketplace_path)
    entries = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("marketplace plugins must be a list")
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("name") == plugin_name
    ]
    if len(matches) != 1:
        raise ValueError(
            f"marketplace must contain exactly one {plugin_name!r} entry"
        )
    entry = matches[0]
    source = entry.get("source")
    if not isinstance(source, dict) or source.get("source") != "local":
        raise ValueError("marketplace plugin source must be local")
    raw_path = str(source.get("path") or "")
    relative = _lexical_relative_path(raw_path)
    parent = marketplace_path.parent
    if parent.name == "plugins" and parent.parent.name == ".agents":
        root = parent.parent.parent
    else:
        root = parent
    root = root.resolve(strict=True)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        try:
            component_stat = os.lstat(current)
        except OSError as exc:
            raise ValueError(
                f"marketplace source parent is unavailable: {raw_path!r}"
            ) from exc
        if (
            _is_link_or_reparse(component_stat)
            or not stat.S_ISDIR(component_stat.st_mode)
        ):
            raise ValueError(
                f"marketplace source parent is unsafe: {raw_path!r}"
            )
    candidate = root.joinpath(*relative.parts)
    try:
        candidate_stat = os.lstat(candidate)
    except OSError as exc:
        raise ValueError(
            f"marketplace source is unavailable: {raw_path!r}"
        ) from exc
    if not (
        stat.S_ISDIR(candidate_stat.st_mode)
        or _is_link_or_reparse(candidate_stat)
    ):
        raise ValueError(
            f"marketplace source is not a directory: {raw_path!r}"
        )
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(
            f"marketplace source does not resolve to a directory: {raw_path!r}"
        )
    return resolved, entry


def verify_install(
    source: Path,
    installed: Path,
    *,
    marketplace: Path | None = None,
) -> list[GateIssue]:
    issues: list[GateIssue] = []
    source_input = Path(os.path.abspath(source.expanduser()))
    installed_input = Path(os.path.abspath(installed.expanduser()))
    for label, candidate in (
        ("source", source_input),
        ("installed", installed_input),
    ):
        reparse = _absolute_reparse_component(candidate)
        if reparse is not None:
            issues.append(
                GateIssue(
                    (
                        "SOURCE_ROOT_REPARSE"
                        if label == "source"
                        else "INSTALLED_ROOT_REPARSE"
                    ),
                    str(candidate),
                    f"{label} root contains reparse component {reparse}",
                )
            )
    if issues:
        return issues
    if source_input.exists() and installed_input.exists():
        try:
            aliased = os.path.samefile(source_input, installed_input)
        except OSError:
            aliased = False
        if aliased:
            return [
                GateIssue(
                    "INSTALLED_ROOT_ALIAS",
                    str(installed_input),
                    "source and installed roots identify the same directory",
                )
            ]
    source = source_input.resolve()
    installed = installed_input.resolve()
    if source in installed.parents or installed in source.parents:
        return [
            GateIssue(
                "INSTALLED_ROOT_OVERLAP",
                str(installed),
                (
                    "source and installed roots must be disjoint; ancestor or "
                    "descendant installs can be hidden by payload noise rules"
                ),
            )
        ]
    try:
        source_manifest_path, _ = _repository_reference(
            source,
            ".codex-plugin/plugin.json",
            expect="file",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return [
            GateIssue(
                "PLUGIN_MANIFEST_MISSING",
                str(source),
                str(exc),
            )
        ]
    try:
        manifest = _load_json(source_manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        return [
            GateIssue(
                "PLUGIN_MANIFEST_INVALID",
                str(source_manifest_path),
                str(exc),
            )
        ]
    if not isinstance(manifest, Mapping):
        return [
            GateIssue(
                "PLUGIN_MANIFEST_INVALID",
                str(source_manifest_path),
                "source manifest root must be an object",
            )
        ]
    name = str(manifest.get("name") or "")
    version = str(manifest.get("version") or "")
    if marketplace is not None:
        try:
            resolved, entry = marketplace_source(marketplace, name)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues.append(
                GateIssue("MARKETPLACE_INVALID", str(marketplace), str(exc))
            )
        else:
            if os.path.normcase(str(resolved)) != os.path.normcase(str(source)):
                issues.append(
                    GateIssue(
                        "MARKETPLACE_SOURCE_DRIFT",
                        str(marketplace),
                        f"entry resolves to {resolved}, expected {source}",
                    )
                )
            policy = entry.get("policy")
            if not isinstance(policy, dict) or not {
                "installation",
                "authentication",
            }.issubset(policy):
                issues.append(
                    GateIssue(
                        "MARKETPLACE_POLICY_INCOMPLETE",
                        str(marketplace),
                        "entry needs installation and authentication policy",
                    )
                )
            else:
                installation = policy.get("installation")
                authentication = policy.get("authentication")
                if installation not in MARKETPLACE_INSTALLATION_POLICIES:
                    issues.append(
                        GateIssue(
                            "MARKETPLACE_POLICY_INVALID",
                            str(marketplace),
                            (
                                "policy.installation must be one of "
                                f"{sorted(MARKETPLACE_INSTALLATION_POLICIES)}, "
                                f"got {installation!r}"
                            ),
                        )
                    )
                if authentication not in MARKETPLACE_AUTHENTICATION_POLICIES:
                    issues.append(
                        GateIssue(
                            "MARKETPLACE_POLICY_INVALID",
                            str(marketplace),
                            (
                                "policy.authentication must be one of "
                                f"{sorted(MARKETPLACE_AUTHENTICATION_POLICIES)}, "
                                f"got {authentication!r}"
                            ),
                        )
                    )
            category = entry.get("category")
            if not isinstance(category, str) or not category.strip():
                issues.append(
                    GateIssue(
                        "MARKETPLACE_CATEGORY_INVALID",
                        str(marketplace),
                        "entry category must be a non-empty string",
                    )
                )
    try:
        installed_manifest_path, _ = _repository_reference(
            installed,
            ".codex-plugin/plugin.json",
            expect="file",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        issues.append(
            GateIssue(
                "INSTALLED_MANIFEST_MISSING",
                str(installed),
                f"installed cache is not a safe plugin payload: {exc}",
            )
        )
        return issues
    try:
        installed_manifest = _load_json(installed_manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(
            GateIssue(
                "INSTALLED_MANIFEST_INVALID",
                str(installed_manifest_path),
                str(exc),
            )
        )
        return issues
    if not isinstance(installed_manifest, Mapping):
        issues.append(
            GateIssue(
                "INSTALLED_MANIFEST_INVALID",
                str(installed_manifest_path),
                "installed manifest root must be an object",
            )
        )
        return issues
    if installed_manifest.get("name") != name:
        issues.append(
            GateIssue(
                "INSTALLED_NAME_MISMATCH",
                str(installed_manifest_path),
                f"installed name does not match {name!r}",
            )
        )
    if installed_manifest.get("version") != version:
        issues.append(
            GateIssue(
                "INSTALLED_VERSION_MISMATCH",
                str(installed_manifest_path),
                (
                    f"installed version {installed_manifest.get('version')!r} "
                    f"does not match source {version!r}"
                ),
            )
        )
    try:
        comparison = compare_install_tree(source, installed)
    except (OSError, RuntimeError, ValueError) as exc:
        issues.append(
            GateIssue(
                "INSTALLED_TREE_COMPARE_FAILED",
                str(installed),
                f"{type(exc).__name__}: {exc}",
            )
        )
        return issues
    for code, values in (
        ("INSTALLED_FILE_MISSING", comparison.missing),
        ("INSTALLED_FILE_MISMATCH", comparison.mismatched),
        ("INSTALLED_FILE_UNEXPECTED", comparison.unexpected),
    ):
        for relative in values:
            issues.append(GateIssue(code, relative, "source/install payload drift"))
    return issues


def _extract_package_payload(root: Path, temporary_root: Path) -> Path:
    root = root.resolve()
    files = payload_files(root)
    entries = {
        entry.path: entry
        for entry in _tracked_paths(root)
        if entry.stage == 0
    }
    archive_path = temporary_root / "plugin.zip"
    extracted = temporary_root / "installed"
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for relative in files:
            info = zipfile.ZipInfo(relative)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(
                info,
                _payload_entry_bytes(root, entries[relative]),
            )
    extracted.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        seen_members: set[str] = set()
        for member in archive.infolist():
            relative = PurePosixPath(member.filename)
            if (
                member.filename in seen_members
                or relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise ValueError(
                    f"archive member path is unsafe: {member.filename}"
                )
            seen_members.add(member.filename)
            current = extracted
            for part in relative.parts[:-1]:
                current /= part
                if not current.exists():
                    current.mkdir()
                file_stat = os.lstat(current)
                if (
                    _is_link_or_reparse(file_stat)
                    or not stat.S_ISDIR(file_stat.st_mode)
                    or extracted.resolve() not in current.resolve().parents
                ):
                    raise ValueError(
                        "archive parent path is unsafe: "
                        f"{member.filename}"
                    )
            target = current / relative.name
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= int(getattr(os, "O_BINARY", 0))
            flags |= int(getattr(os, "O_NOFOLLOW", 0))
            descriptor = os.open(target, flags, 0o644)
            try:
                data = archive.read(member)
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("archive member write made no progress")
                    view = view[written:]
            finally:
                os.close(descriptor)
            problem = _payload_path_problem(
                extracted,
                TrackedPath(
                    path=relative.as_posix(),
                    mode=None,
                    stage=0,
                ),
            )
            if problem is not None:
                raise ValueError(problem.render())
    return extracted


def package_roundtrip(root: Path) -> TreeComparison:
    root = root.resolve()
    files = payload_files(root)
    with tempfile.TemporaryDirectory(prefix="plot-rag-release-") as temporary:
        extracted = _extract_package_payload(root, Path(temporary))
        return compare_install_tree(root, extracted, files=files)


def _extracted_mcp_entrypoint(
    extracted: Path,
) -> tuple[Mapping[str, Any], str, tuple[str, ...], Path, str]:
    manifest_path, _manifest_relative = _repository_reference(
        extracted,
        ".codex-plugin/plugin.json",
        expect="file",
    )
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, Mapping):
        raise ValueError("extracted plugin manifest root must be an object")
    mcp_path, mcp_relative = _repository_reference(
        extracted,
        str(manifest.get("mcpServers") or ""),
        expect="file",
    )
    mcp = _load_json(mcp_path)
    if not isinstance(mcp, Mapping) or set(mcp) != {"mcpServers"}:
        raise ValueError(
            "extracted MCP manifest root must contain exactly mcpServers"
        )
    servers = mcp.get("mcpServers")
    if not isinstance(servers, Mapping) or set(servers) != {
        EXPECTED_MCP_SERVER_NAME
    }:
        raise ValueError(
            "extracted MCP manifest must contain exactly "
            f"{EXPECTED_MCP_SERVER_NAME!r}"
        )
    server = servers[EXPECTED_MCP_SERVER_NAME]
    if not isinstance(server, Mapping):
        raise ValueError("extracted MCP server entry must be an object")
    if set(server) != {"command", "args", "cwd"}:
        raise ValueError(
            "extracted MCP server must contain exactly command, args, and cwd"
        )
    command = server.get("command")
    arguments = server.get("args")
    cwd_value = str(server.get("cwd") or ".")
    if (
        not isinstance(command, str)
        or not _is_python_command(command)
        or PureWindowsPath(command).name != command
        or not isinstance(arguments, list)
        or not all(isinstance(argument, str) for argument in arguments)
        or tuple(arguments) != EXPECTED_MCP_ARGUMENTS
        or cwd_value != EXPECTED_MCP_CWD
    ):
        raise ValueError(
            "extracted MCP entrypoint does not match the verified direct "
            "Python command, arguments, and cwd contract"
        )
    cwd, _cwd_relative = _repository_reference(
        extracted,
        cwd_value,
        expect="dir",
    )
    _repository_reference(
        extracted,
        EXPECTED_MCP_ARGUMENTS[-1],
        base=EXPECTED_MCP_CWD,
        expect="file",
        require_payload_file=False,
    )
    return manifest, command, tuple(arguments), cwd, mcp_relative


def _smoke_python_command(
    command: str,
    environment: Mapping[str, str],
) -> str:
    """Resolve the verified bare Python command without making smoke host-specific."""
    if shutil.which(command, path=environment.get("PATH")) is not None:
        return command
    return sys.executable


def package_smoke(root: Path) -> list[GateIssue]:
    root = root.resolve()
    issues: list[GateIssue] = []
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PLOT_RAG_PROJECT_ROOT", None)
    with tempfile.TemporaryDirectory(prefix="plot-rag-smoke-") as temporary:
        temporary_root = Path(temporary)
        try:
            extracted = _extract_package_payload(root, temporary_root)
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
            return [
                GateIssue(
                    "PACKAGE_SMOKE_BUILD_FAILED",
                    ".",
                    f"plugin payload could not be packaged: {exc}",
                )
            ]
        try:
            (
                manifest,
                mcp_command,
                mcp_arguments,
                mcp_cwd,
                mcp_relative,
            ) = _extracted_mcp_entrypoint(extracted)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return [
                GateIssue(
                    "PACKAGE_SMOKE_BUILD_FAILED",
                    ".codex-plugin/plugin.json",
                    (
                        "extracted manifest/MCP entrypoint could not be "
                        f"resolved: {exc}"
                    ),
                )
            ]
        mcp_launch_command = _smoke_python_command(mcp_command, environment)
        version = str(manifest.get("version") or "")
        base = _semantic_base(version) or version
        try:
            cli = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    str(extracted / "scripts" / "plot_state.py"),
                    "--version",
                ],
                cwd=extracted,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            issues.append(
                GateIssue(
                    "PACKAGE_SMOKE_CLI_FAILED",
                    "scripts/plot_state.py",
                    f"{type(exc).__name__}: {exc}",
                )
            )
            cli = None
        if (
            cli is not None
            and (
                cli.returncode != 0
                or f"plot-rag-gate {base} " not in cli.stdout
            )
        ):
            issues.append(
                GateIssue(
                    "PACKAGE_SMOKE_CLI_FAILED",
                    "scripts/plot_state.py",
                    (
                        f"returncode={cli.returncode}; "
                        f"stdout={cli.stdout.strip()!r}; "
                        f"stderr={cli.stderr.strip()!r}"
                    ),
                )
            )

        project = temporary_root / "fixture-project"
        config_dir = project / ".plot-rag"
        try:
            config_dir.mkdir(parents=True)
            shutil.copyfile(
                extracted / "templates" / "config.v3.json",
                config_dir / "config.json",
            )
        except OSError as exc:
            issues.append(
                GateIssue(
                    "PACKAGE_SMOKE_MCP_FAILED",
                    "templates/config.v3.json",
                    f"fixture project could not be prepared: {exc}",
                )
            )
            return issues
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "release-smoke", "version": "1"},
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "list_power_systems",
                    "arguments": {"project_root": str(project)},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "validate_power_spec_change",
                    "arguments": {
                        "power_spec": {
                            "schema_version": "plot-rag-power/v1",
                            "power_systems": [
                                {
                                    "namespace": "release-smoke.power",
                                    "name": "Release Smoke Power",
                                    "profile": "mundane",
                                }
                            ],
                            "actor_power_bootstrap": [],
                        }
                    },
                },
            },
        ]
        try:
            mcp = subprocess.run(
                [mcp_launch_command, *mcp_arguments],
                input="\n".join(
                    json.dumps(message, ensure_ascii=False)
                    for message in messages
                )
                + "\n",
                cwd=mcp_cwd,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            issues.append(
                GateIssue(
                    "PACKAGE_SMOKE_MCP_FAILED",
                    mcp_relative,
                    f"{type(exc).__name__}: {exc}",
                )
            )
            return issues
        responses: dict[int, Mapping[str, Any]] = {}
        for line_number, line in enumerate(mcp.stdout.splitlines(), start=1):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                issues.append(
                    GateIssue(
                        "PACKAGE_SMOKE_MCP_FAILED",
                        mcp_relative,
                        f"invalid JSON-RPC output at line {line_number}: {exc}",
                    )
                )
                continue
            if isinstance(payload, dict) and type(payload.get("id")) is int:
                response_id = payload["id"]
                if response_id in responses:
                    issues.append(
                        GateIssue(
                            "PACKAGE_SMOKE_MCP_FAILED",
                            mcp_relative,
                            f"duplicate JSON-RPC response id: {response_id}",
                        )
                    )
                responses[response_id] = payload
        initialize_response = dict(responses.get(1) or {})
        tools_response = dict(responses.get(2) or {})
        call_response = dict(responses.get(3) or {})
        power_spec_response = dict(responses.get(4) or {})
        initialize_value = initialize_response.get("result")
        tools_value = tools_response.get("result")
        tool_call_value = call_response.get("result")
        power_spec_value = power_spec_response.get("result")
        initialize = (
            dict(initialize_value) if isinstance(initialize_value, Mapping) else {}
        )
        tools_result = dict(tools_value) if isinstance(tools_value, Mapping) else {}
        tool_call = (
            dict(tool_call_value)
            if isinstance(tool_call_value, Mapping)
            else {}
        )
        power_spec_call = (
            dict(power_spec_value)
            if isinstance(power_spec_value, Mapping)
            else {}
        )
        server_info_value = initialize.get("serverInfo")
        server_info = (
            dict(server_info_value)
            if isinstance(server_info_value, Mapping)
            else {}
        )
        structured = tool_call.get("structuredContent")
        power_spec_structured = power_spec_call.get("structuredContent")
        tool_names = {
            str(tool.get("name") or "")
            for tool in tools_result.get("tools") or []
            if isinstance(tool, Mapping)
        }
        required_power_spec_tools = set(POWER_SPEC_REQUIRED_MCP_TOOLS)
        if (
            mcp.returncode != 0
            or initialize_response.get("error")
            or tools_response.get("error")
            or call_response.get("error")
            or power_spec_response.get("error")
            or str(server_info.get("version") or "") != base
            or "list_power_systems" not in tool_names
            or not required_power_spec_tools.issubset(tool_names)
            or tool_call.get("isError")
            or power_spec_call.get("isError")
            or not isinstance(structured, Mapping)
            or not isinstance(power_spec_structured, Mapping)
            or str(structured.get("status") or "") == "ERROR"
            or str(power_spec_structured.get("status") or "") != "ready"
            or power_spec_structured.get("read_only") is not True
            or not isinstance(power_spec_structured.get("summary"), Mapping)
            or int(
                power_spec_structured.get("summary", {}).get(
                    "event_count",
                    0,
                )
            )
            != 1
            or not isinstance(structured.get("systems"), list)
            or 3 not in responses
            or 4 not in responses
        ):
            issues.append(
                GateIssue(
                    "PACKAGE_SMOKE_MCP_FAILED",
                    mcp_relative,
                    (
                        f"returncode={mcp.returncode}; "
                        f"responses={sorted(responses)}; "
                        f"tools={len(tool_names)}; "
                        f"stderr={mcp.stderr.strip()!r}"
                    ),
                )
            )
    return issues


def _default_cachebuster_helper() -> Path:
    candidates: list[Path] = []
    configured_home = os.environ.get("CODEX_HOME")
    if configured_home:
        candidates.append(Path(configured_home).expanduser())
    candidates.append(Path.home() / ".codex")
    for codex_home in candidates:
        helper = (
            codex_home
            / "skills"
            / ".system"
            / "plugin-creator"
            / "scripts"
            / "update_plugin_cachebuster.py"
        )
        if helper.is_file():
            return helper.resolve()
    raise FileNotFoundError(
        "official plugin-creator update_plugin_cachebuster.py was not found"
    )


def update_plugin_cachebuster(
    root: Path,
    *,
    helper: Path | None = None,
) -> tuple[str, str]:
    root = root.resolve()
    manifest_path = root / ".codex-plugin" / "plugin.json"
    original_bytes = manifest_path.read_bytes()
    original = json.loads(original_bytes.decode("utf-8"))
    if not isinstance(original, Mapping):
        raise ValueError("plugin manifest root must be an object")
    original_version = str(original.get("version") or "")
    original_base = _semantic_base(original_version)
    if original_base is None:
        raise ValueError("plugin manifest version must be valid SemVer")
    helper_path = (helper or _default_cachebuster_helper()).resolve()
    if not helper_path.is_file():
        raise FileNotFoundError(f"cachebuster helper is missing: {helper_path}")

    combined_output: list[str] = []
    try:
        for attempt in range(2):
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    str(helper_path),
                    str(root),
                ],
                cwd=root,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            if completed.stdout.strip():
                combined_output.append(completed.stdout.strip())
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise RuntimeError(
                    f"official cachebuster helper failed: {detail}"
                )

            normalized = (
                manifest_path.read_bytes()
                .decode("utf-8")
                .replace("\r\n", "\n")
                .replace("\r", "\n")
                .encode("utf-8")
            )
            manifest_path.write_bytes(normalized)
            updated = json.loads(normalized.decode("utf-8"))
            if not isinstance(updated, Mapping):
                raise ValueError("updated plugin manifest root must be an object")
            updated_version = str(updated.get("version") or "")
            updated_base = _semantic_base(updated_version)
            updated_token = _cachebuster_token(updated_version)
            if updated_base != original_base:
                raise ValueError(
                    "official cachebuster helper changed the semantic base version"
                )
            if not _is_release_cachebuster(updated_token):
                raise ValueError(
                    "official cachebuster helper did not produce a 14-digit UTC token"
                )
            if b"\r" in manifest_path.read_bytes():
                raise ValueError("plugin manifest still contains CR bytes")
            if updated_version != original_version:
                return updated_version, "\n".join(combined_output)
            if attempt == 0:
                time.sleep(1.05)
        raise RuntimeError("cachebuster helper did not produce a new version token")
    except BaseException:
        manifest_path.write_bytes(original_bytes)
        raise


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("provider_sk", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}")),
    (
        "github_token",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|"
            r"github_pat_[A-Za-z0-9_]{40,})\b"
        ),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "bearer_token",
        re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]{20,})"),
    ),
    (
        "api_key_assignment",
        re.compile(
            r"(?i)\b(?:SILICONFLOW_API_KEY|OPENAI_API_KEY|"
            r"ANTHROPIC_API_KEY|GITHUB_TOKEN|GH_TOKEN)"
            r"\s*[=:]\s*[\"']?([^\s\"']{16,})"
        ),
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
)


def _secret_candidate(kind: str, matched: re.Match[str]) -> str:
    if kind in {"bearer_token", "api_key_assignment"} and matched.lastindex:
        return matched.group(matched.lastindex)
    return matched.group(0)


def _explicit_secret_fixture(
    *,
    kind: str,
    candidate: str,
    path: str,
    line: str,
) -> bool:
    normalized_path = path.replace("\\", "/")
    if (
        kind != "api_key_assignment"
        or candidate not in EXPLICIT_SECRET_FIXTURE_VALUES
        or normalized_path not in EXPLICIT_SECRET_FIXTURE_PATHS
    ):
        return False
    assignment = re.fullmatch(
        r"(?i)\s*(?:export\s+)?"
        r"(?:SILICONFLOW_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|"
        r"GITHUB_TOKEN|GH_TOKEN)"
        r"\s*[=:]\s*[\"']?"
        + re.escape(candidate)
        + r"[\"']?\s*",
        line,
    )
    return assignment is not None


def _mask_secret(candidate: str) -> str:
    fingerprint = hashlib.sha256(candidate.encode("utf-8", "replace")).hexdigest()
    if candidate.startswith("-----BEGIN "):
        masked = "PRIVATE_KEY_HEADER"
    elif len(candidate) >= 10:
        masked = f"{candidate[:4]}…{candidate[-4:]}"
    else:
        masked = "<redacted>"
    return f"{masked} len={len(candidate)} sha256[:12]={fingerprint[:12]}"


def _scan_text(
    text: str,
    *,
    scope: str,
    path: str,
) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in SECRET_PATTERNS:
            for matched in pattern.finditer(line):
                candidate = _secret_candidate(kind, matched)
                if _explicit_secret_fixture(
                    kind=kind,
                    candidate=candidate,
                    path=path,
                    line=line,
                ):
                    continue
                findings.append(
                    SecretFinding(
                        scope=scope,
                        path=path,
                        line=line_number,
                        kind=kind,
                        masked=_mask_secret(candidate),
                    )
                )
    return findings


def scan_source_secrets(root: Path) -> list[SecretFinding]:
    root = root.resolve()
    findings: list[SecretFinding] = []
    for entry in _tracked_paths(root):
        if entry.stage != 0:
            raise RuntimeError(
                f"SECRET_SCAN_INDEX_CONFLICT: {entry.path}"
            )
        data = _git_blob_bytes(root, entry)
        if b"\0" in data:
            continue
        findings.extend(
            _scan_text(
                data.decode("utf-8", "replace"),
                scope="git-index",
                path=entry.path,
            )
        )
    for relative in untracked_release_files(root):
        try:
            data = _read_payload_bytes(root, relative)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"SECRET_SCAN_UNSAFE_PATH: {relative}: {exc}"
            ) from exc
        if b"\0" in data:
            continue
        findings.extend(
            _scan_text(
                data.decode("utf-8", "replace"),
                scope="untracked-worktree",
                path=relative,
            )
        )
    return findings


def scan_history_secrets(root: Path) -> list[SecretFinding]:
    root = root.resolve()
    commits_result = _run(["git", "rev-list", "--all"], cwd=root, check=False)
    if commits_result.returncode != 0:
        raise RuntimeError(commits_result.stderr.strip() or "git history unavailable")
    grep_expression = (
        r"sk-[A-Za-z0-9_-]{20,}|"
        r"gh[pousr]_[A-Za-z0-9]{30,}|"
        r"github_pat_[A-Za-z0-9_]{40,}|"
        r"(AKIA|ASIA)[0-9A-Z]{16}|"
        r"AIza[0-9A-Za-z_-]{35}|"
        r"Bearer[[:space:]]+[A-Za-z0-9._~+/=-]{20,}|"
        r"(SILICONFLOW_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|"
        r"GITHUB_TOKEN|GH_TOKEN)[[:space:]]*[=:][[:space:]]*"
        r"[^[:space:]\"']{16,}|"
        r"BEGIN[[:space:]]+(RSA[[:space:]]+|EC[[:space:]]+|"
        r"OPENSSH[[:space:]]+|DSA[[:space:]]+)?PRIVATE[[:space:]]+KEY"
    )
    findings: list[SecretFinding] = []
    seen: set[tuple[str, str, int, str, str]] = set()
    for commit in commits_result.stdout.split():
        result = _run(
            ["git", "grep", "-I", "-n", "-E", grep_expression, commit, "--"],
            cwd=root,
            check=False,
        )
        for raw in result.stdout.splitlines():
            parts = raw.split(":", 3)
            if len(parts) != 4:
                continue
            revision, path, line_text, text = parts
            try:
                original_line = int(line_text)
            except ValueError:
                continue
            for finding in _scan_text(
                text,
                scope=f"git:{revision[:12]}",
                path=path,
            ):
                key = (
                    revision,
                    path,
                    original_line,
                    finding.kind,
                    finding.masked,
                )
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    SecretFinding(
                        scope=finding.scope,
                        path=finding.path,
                        line=original_line,
                        kind=finding.kind,
                        masked=finding.masked,
                    )
                )
    return findings


def _print_issues(issues: Iterable[GateIssue]) -> None:
    for issue in issues:
        print(issue.render())


def _print_comparison(comparison: TreeComparison) -> None:
    for label, values in (
        ("missing", comparison.missing),
        ("mismatched", comparison.mismatched),
        ("unexpected", comparison.unexpected),
    ):
        for relative in values:
            print(f"{label} | {relative}")


def _command_validate(args: argparse.Namespace) -> int:
    issues = validate_source(Path(args.root))
    if issues:
        _print_issues(issues)
        print(f"release-gate validate: FAIL ({len(issues)} issue(s))")
        return 1
    print("release-gate validate: PASS")
    return 0


def _command_secrets(args: argparse.Namespace) -> int:
    root = Path(args.root)
    try:
        findings = scan_source_secrets(root)
        if args.history:
            findings.extend(scan_history_secrets(root))
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            GateIssue(
                "SECRET_SCAN_FAILED",
                ".",
                f"{type(exc).__name__}: {exc}",
            ).render()
        )
        print("release-gate secrets: FAIL")
        return 1
    for finding in findings:
        print(finding.render())
    if findings:
        print(f"release-gate secrets: FAIL ({len(findings)} finding(s))")
        return 1
    scope = "source tree + reachable git history" if args.history else "source tree"
    print(f"release-gate secrets: PASS ({scope})")
    return 0


def _command_roundtrip(args: argparse.Namespace) -> int:
    try:
        comparison = package_roundtrip(Path(args.root))
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        _print_issues(
            [
                GateIssue(
                    "PACKAGE_ROUNDTRIP_FAILED",
                    ".",
                    f"{type(exc).__name__}: {exc}",
                )
            ]
        )
        print("release-gate roundtrip: FAIL")
        return 1
    if not comparison.ok:
        _print_comparison(comparison)
        print("release-gate roundtrip: FAIL")
        return 1
    print("release-gate roundtrip: PASS")
    return 0


def _command_smoke(args: argparse.Namespace) -> int:
    issues = package_smoke(Path(args.root))
    if issues:
        _print_issues(issues)
        print(f"release-gate smoke: FAIL ({len(issues)} issue(s))")
        return 1
    print("release-gate smoke: PASS")
    return 0


def _command_cachebuster(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest_relative = ".codex-plugin/plugin.json"
    manifest_path = root / manifest_relative
    original_worktree: bytes | None = None
    original_entry: TrackedPath | None = None
    try:
        preflight_issues = _validate_payload(root)
        if preflight_issues:
            raise RuntimeError(
                "cachebuster preflight failed:\n"
                + "\n".join(issue.render() for issue in preflight_issues)
            )
        original_worktree = manifest_path.read_bytes()
        original_entry = next(
            (
                entry
                for entry in _tracked_paths(root)
                if entry.path == manifest_relative and entry.stage == 0
            ),
            None,
        )
        if original_entry is None:
            raise RuntimeError("plugin manifest is not staged in the Git index")
        version, helper_output = update_plugin_cachebuster(
            root,
            helper=Path(args.helper) if args.helper else None,
        )
        staged = _run(
            ["git", "add", "--", manifest_relative],
            cwd=root,
            check=False,
        )
        if staged.returncode != 0:
            raise RuntimeError(
                staged.stderr.strip() or "failed to stage plugin manifest"
            )
        validation_issues = validate_source(root)
        if validation_issues:
            raise RuntimeError(
                "post-cachebuster validation failed:\n"
                + "\n".join(issue.render() for issue in validation_issues)
            )
        comparison = package_roundtrip(root)
        if not comparison.ok:
            raise RuntimeError(
                "post-cachebuster roundtrip failed: "
                f"missing={comparison.missing}, "
                f"mismatched={comparison.mismatched}, "
                f"unexpected={comparison.unexpected}"
            )
        smoke_issues = package_smoke(root)
        if smoke_issues:
            raise RuntimeError(
                "post-cachebuster smoke failed:\n"
                + "\n".join(issue.render() for issue in smoke_issues)
            )
    except BaseException as exc:
        rollback_error = ""
        if original_worktree is not None:
            try:
                manifest_path.write_bytes(original_worktree)
            except OSError as rollback_exc:
                rollback_error = (
                    f"; worktree rollback failed: "
                    f"{type(rollback_exc).__name__}: {rollback_exc}"
                )
        if (
            original_entry is not None
            and original_entry.mode
            and original_entry.object_id
        ):
            rollback = _run(
                [
                    "git",
                    "update-index",
                    "--cacheinfo",
                    (
                        f"{original_entry.mode},"
                        f"{original_entry.object_id},"
                        f"{manifest_relative}"
                    ),
                ],
                cwd=root,
                check=False,
            )
            if rollback.returncode != 0:
                detail = rollback.stderr.strip() or rollback.stdout.strip()
                rollback_error += f"; index rollback failed: {detail}"
        if not isinstance(exc, Exception):
            if rollback_error:
                print(
                    GateIssue(
                        "CACHEBUSTER_ROLLBACK_FAILED",
                        ".codex-plugin/plugin.json",
                        rollback_error.lstrip("; "),
                    ).render(),
                    file=sys.stderr,
                )
            raise
        _print_issues(
            [
                GateIssue(
                    "CACHEBUSTER_UPDATE_FAILED",
                    ".codex-plugin/plugin.json",
                    f"{type(exc).__name__}: {exc}{rollback_error}",
                )
            ]
        )
        print("release-gate cachebuster: FAIL")
        return 1
    if helper_output:
        print(helper_output)
    print("release-gate validate: PASS")
    print("release-gate roundtrip: PASS")
    print("release-gate smoke: PASS")
    print(
        f"release-gate cachebuster: PASS ({version}, LF normalized and staged)"
    )
    return 0


def _command_verify_install(args: argparse.Namespace) -> int:
    issues = verify_install(
        Path(args.source),
        Path(args.installed),
        marketplace=Path(args.marketplace) if args.marketplace else None,
    )
    if issues:
        _print_issues(issues)
        print(f"release-gate verify-install: FAIL ({len(issues)} issue(s))")
        return 1
    print("release-gate verify-install: PASS")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate source contracts")
    validate.add_argument("--root", default=".")
    validate.set_defaults(func=_command_validate)

    secrets = subparsers.add_parser(
        "secrets",
        help="scan tracked source and optionally reachable git history",
    )
    secrets.add_argument("--root", default=".")
    secrets.add_argument("--history", action="store_true")
    secrets.set_defaults(func=_command_secrets)

    roundtrip = subparsers.add_parser(
        "roundtrip",
        help="build and compare a deterministic plugin payload",
    )
    roundtrip.add_argument("--root", default=".")
    roundtrip.set_defaults(func=_command_roundtrip)

    smoke = subparsers.add_parser(
        "smoke",
        help="package, extract, and start the CLI and MCP payload",
    )
    smoke.add_argument("--root", default=".")
    smoke.set_defaults(func=_command_smoke)

    cachebuster = subparsers.add_parser(
        "cachebuster",
        help="run the official helper and normalize plugin.json to LF bytes",
    )
    cachebuster.add_argument("--root", default=".")
    cachebuster.add_argument(
        "--helper",
        help="override the official helper path for isolated testing",
    )
    cachebuster.set_defaults(func=_command_cachebuster)

    verify = subparsers.add_parser(
        "verify-install",
        help="compare marketplace/source/installed cache without fixed paths",
    )
    verify.add_argument("--source", required=True)
    verify.add_argument("--installed", required=True)
    verify.add_argument("--marketplace")
    verify.set_defaults(func=_command_verify_install)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
