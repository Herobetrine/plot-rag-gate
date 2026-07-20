"""Protocol constants for the proposal-only story initialization engine."""

from __future__ import annotations

from typing import Final


PROTOCOL_V1: Final = "plot-rag-init/v1"
PROTOCOL_V2: Final = "plot-rag-init/v2"
PROTOCOL_AUTO: Final = "auto"
PROTOCOL_VERSION: Final = PROTOCOL_V1
SUPPORTED_PROTOCOL_VERSIONS: Final = (PROTOCOL_V1, PROTOCOL_V2, PROTOCOL_AUTO)
POWER_SCHEMA_VERSION: Final = "plot-rag-power/v1"
DATABASE_SCHEMA_VERSION: Final = 2

MODES: Final = ("new", "ingest", "hybrid")
ROUTING_MODES: Final = ("auto", *MODES)
TARGET_PROFILES: Final = (
    "plot_ready",
    "world_bible",
    "normalize_only",
    "continuity_ready",
)
INTERACTION_PROFILES: Final = ("minimal", "balanced", "deep")

FIELD_STATUSES: Final = (
    "user_confirmed",
    "source_supported",
    "model_proposed",
    "unknown",
    "conflicted",
    "deferred",
    "not_applicable",
)
ORIGINS: Final = (
    "user_input",
    "source_extract",
    "model_suggestion",
    "deterministic_derived",
)
DECISION_STATUSES: Final = ("open", "session_locked", "delegated")
CANON_STATUSES: Final = ("proposed", "accepted", "rejected", "retracted")
SCOPES: Final = ("current", "planned", "historical", "timeless")
KNOWLEDGE_PLANES: Final = (
    "objective",
    "actor_belief",
    "public_narrative",
    "reader_disclosed",
    "author_plan",
)
MODALITIES: Final = ("asserted", "hypothetical", "conditional")

SOURCE_ROLES: Final = ("canon", "setting", "outline", "draft", "note", "reference")
AUTHORITY_TIERS: Final = ("T0", "T1", "T2", "T3", "T4", "T5")
INGEST_POLICIES: Final = ("include", "review", "exclude")
SCOPE_POLICIES: Final = (
    "infer_and_review",
    "planned_only",
    "timeless_candidate",
    "preserve_unknown",
)

ACTIVE_STATUSES: Final = (
    "ACTIVE",
    "NEEDS_INPUT",
    "READY_TO_PROPOSE",
    "PROPOSAL_FROZEN",
    "PAUSED_REMOTE",
    "STALE_SOURCE",
    "STALE_CANON",
)
TERMINAL_STATUSES: Final = ("COMPLETED", "CANCELLED", "SUPERSEDED")

NEW_STAGE_FLOW: Final = (
    "CREATED",
    "DISCOVER",
    "ROUTING",
    "GENRE_CONTRACT",
    "WORLD_CAUSAL_KERNEL",
    "POWER_CAUSAL_KERNEL",
    "ACTOR_ANCHOR",
    "STORY_ENGINE",
    "SERIALIZATION_CONTRACT",
    "NORMALIZE",
    "VALIDATE",
    "REVIEW",
    "READY_TO_PROPOSE",
)
INGEST_STAGE_FLOW: Final = (
    "CREATED",
    "DISCOVER",
    "ROUTING",
    "INVENTORY",
    "CLASSIFY",
    "EXTRACT",
    "CONFLICT",
    "GAP",
    "NORMALIZE",
    "VALIDATE",
    "REVIEW",
    "READY_TO_PROPOSE",
)
HYBRID_STAGE_FLOW: Final = (
    "CREATED",
    "DISCOVER",
    "ROUTING",
    "INVENTORY",
    "CLASSIFY",
    "EXTRACT",
    "CONFLICT",
    "GAP",
    "GENRE_CONTRACT",
    "WORLD_CAUSAL_KERNEL",
    "POWER_CAUSAL_KERNEL",
    "ACTOR_ANCHOR",
    "STORY_ENGINE",
    "SERIALIZATION_CONTRACT",
    "NORMALIZE",
    "VALIDATE",
    "REVIEW",
    "READY_TO_PROPOSE",
)

WORLD_OBJECT_TYPES: Final = (
    "Coordinate",
    "Rule",
    "Stock",
    "Flow",
    "Actor",
    "Relation",
    "Belief",
    "Event",
    "Pressure",
)

WORLD_MODULES: Final = (
    "spacetime_and_communication",
    "natural_and_extraordinary_laws",
    "resources_and_ecology",
    "population_household_livelihood_migration",
    "technology_production_infrastructure",
    "institutions_law_violence_power",
    "economy_property_tax_debt_logistics",
    "culture_identity_religion_ritual_taboo",
    "knowledge_information_secrecy_distortion",
    "historical_inertia_unsettled_debts_memory",
    "actor_goals_capabilities_beliefs_offscreen_plans",
    "cross_class_daily_life",
    "pressure_gradients_thresholds_conflict_generation",
)

RESOLUTION_LEVELS: Final = ("kernel", "regional", "local", "scene", "texture")

PRESSURE_TESTS: Final = (
    ("ordinary_day", "普通一天测试"),
    ("thirty_days_without_protagonist", "三十天无主角测试"),
    ("supply_cut", "断供测试"),
    ("optimal_exploitation", "最优利用测试"),
    ("power_vacuum", "权力真空测试"),
    ("information_leak", "信息泄漏测试"),
    ("cross_class_view", "跨阶层视角测试"),
    ("spacetime_conservation", "时空与守恒测试"),
    ("historical_counterfactual", "历史反事实测试"),
    ("plot_fertility", "剧情繁殖力测试"),
)

MVW_FIELDS: Final = (
    "story_clock",
    "locations_and_routes",
    "base_rules",
    "core_capability",
    "survival_resource_chain",
    "power_scarcity_chain",
    "daily_cycles",
    "infrastructure_bottleneck",
    "formal_institution_chain",
    "power_actors",
    "harmed_group",
    "legitimacy_narrative",
    "important_secret",
    "historical_trauma",
    "pressure_horizons",
    "irreversible_trigger",
)

STANDARD_ARTIFACTS: Final = (
    ("作品合同/题材合同.md", "genre_contract", "题材合同"),
    ("作品合同/连载兑现合同.md", "serialization_contract", "连载兑现合同"),
    ("设定集/世界内核.md", "world_model", "世界内核"),
    ("设定集/时空与地理.md", "world_spacetime", "时空与地理"),
    ("设定集/规则与力量.md", "world_rules", "规则与力量"),
    ("设定集/资源与社会.md", "world_resources", "资源与社会"),
    ("设定集/历史与当前压力.md", "world_pressure", "历史与当前压力"),
    ("角色/角色索引.md", "actor_system", "角色索引"),
    ("剧情/故事发动机.md", "story_engine", "故事发动机"),
    ("剧情/总纲.md", "story_outline", "总纲"),
    ("剧情/未决剧情债务.md", "open_loops", "未决剧情债务"),
    (".plot-rag/config.json", "project_config", "剧情 RAG 配置"),
)

POWER_ARTIFACTS: Final = (
    ("设定集/力量体系/00_体系总览.md", "power_overview", "力量体系总览"),
    ("设定集/力量体系/01_成长轨与阶段图.md", "power_progression", "成长轨与阶段图"),
    ("设定集/力量体系/02_资源与晋升条件.md", "power_resources", "资源与晋升条件"),
    ("设定集/力量体系/03_能力技能与来源.md", "power_abilities", "能力技能与来源"),
    ("设定集/力量体系/04_状态克制与战斗边界.md", "power_counters", "状态克制与战斗边界"),
    ("设定集/力量体系/05_装备血脉契约与职业.md", "power_bindings", "装备血脉契约与职业"),
    ("设定集/力量体系/06_社会权限与阶层后果.md", "power_society", "社会权限与阶层后果"),
    ("设定集/力量体系/07_跨体系换算与术语表.md", "power_bridges", "跨体系换算与术语表"),
)

QUESTION_DEPENDENCIES: Final = {
    "genre-contract": (
        "genre_contract",
        "world_model",
        "actor_system",
        "story_engine",
        "serialization_contract",
    ),
    "world-causal-kernel": (
        "world_model",
        "power_system",
        "actor_system",
        "story_engine",
        "serialization_contract",
    ),
    "power-causal-kernel": (
        "power_system",
        "actor_system",
        "story_engine",
        "serialization_contract",
    ),
    "story-engine": ("actor_system", "story_engine", "serialization_contract"),
}

DEFAULT_EXCLUDED_PARTS: Final = frozenset(
    {
        ".git",
        ".plot-rag-init",
        "init-sessions",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "dist",
        "build",
        "coverage",
        "logs",
        "log",
        "cache",
        ".cache",
        "backups",
        "backup",
    }
)

GENERATED_FILE_NAMES: Final = frozenset(
    {
        "index.sqlite3",
        "state.sqlite3",
        "init.sqlite3",
        "state_snapshot.json",
        "accepted-source-manifest.json",
        "completion-receipt.json",
    }
)

TEXT_EXTENSIONS: Final = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".csv",
        ".rst",
        ".log",
    }
)

MAX_SOURCE_BYTES: Final = 16 * 1024 * 1024
