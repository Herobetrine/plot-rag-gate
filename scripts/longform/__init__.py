"""Long-form webnovel retrieval, memory, projection, and craft-method engine.

This package is deliberately independent from the legacy plugin entry points.
It exposes deterministic building blocks that can be wired into the public CLI
and MCP surfaces after their lifecycle contracts are stable.
"""

from .authority import (
    AUTHORITY_INDEX_SCHEMA_VERSION,
    AuthorityIndex,
    AuthorityIndexError,
    AuthoritySource,
)
from .benchmarking import (
    BENCHMARK_MANIFEST_VERSION,
    POWER_BENCHMARK_MANIFEST_VERSION,
    load_annotation_manifest,
    rank_labeled_candidates,
    run_annotation_benchmark,
    run_power_annotation_benchmark,
    validate_annotation_manifest,
    validate_power_annotation_manifest,
)
from .continuity import (
    ContextContractBuilder,
    ContinuityNeed,
    decompose_continuity_needs,
)
from .memory import AcceptedSummaryStore, LayeredMemoryStore
from .methods import ProjectPatternStore, WebnovelMethodPack
from .projections import (
    PROJECTION_NAMES,
    ProjectionJournal,
    ProjectionRunError,
    stable_normalized_hash,
)

__all__ = [
    "AUTHORITY_INDEX_SCHEMA_VERSION",
    "BENCHMARK_MANIFEST_VERSION",
    "POWER_BENCHMARK_MANIFEST_VERSION",
    "PROJECTION_NAMES",
    "AcceptedSummaryStore",
    "AuthorityIndex",
    "AuthorityIndexError",
    "AuthoritySource",
    "ContextContractBuilder",
    "ContinuityNeed",
    "LayeredMemoryStore",
    "ProjectPatternStore",
    "ProjectionJournal",
    "ProjectionRunError",
    "WebnovelMethodPack",
    "decompose_continuity_needs",
    "load_annotation_manifest",
    "rank_labeled_candidates",
    "run_annotation_benchmark",
    "run_power_annotation_benchmark",
    "stable_normalized_hash",
    "validate_annotation_manifest",
    "validate_power_annotation_manifest",
]
