"""Deterministic end-to-end benchmark for generated continuity proposals.

The benchmark deliberately starts from ``assistant_text``.  Proposal blocks
are parsed from that text, entity mentions are resolved against a per-case
catalog, evidence spans are checked against the original prose, and the
resulting event is passed through the production continuity normalizer plus a
small benchmark semantic gate.  Nothing is written to a user project.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:  # ``scripts.longform`` package import.
    from ..continuity import ContinuityService, HostApprovalAuthority
    from ..continuity.validators import ContinuityError, normalize_event
except ImportError:  # Top-level ``longform`` with ``scripts`` on sys.path.
    from continuity import ContinuityService, HostApprovalAuthority
    from continuity.validators import ContinuityError, normalize_event


BENCHMARK_MANIFEST_VERSION = 1
POWER_BENCHMARK_MANIFEST_VERSION = 1
POWER_BENCHMARK_SUITE = "plot-rag-power"
POWER_BENCHMARK_PROFILES = (
    "cultivation",
    "magic",
    "skill_tree",
    "game",
    "martial",
    "superpower",
    "bloodline",
    "technology",
    "contract_summoning",
    "system_assist",
    "hybrid",
    "mundane",
)
POWER_EVENT_FAMILIES = (
    "ability",
    "progression",
    "resource",
    "status_effect",
    "power_binding",
    "qualification",
    "power_observation",
)
_POWER_PROJECTION_VOLATILE_FIELDS = frozenset(
    {
        "approval_id",
        "binding_hash",
        "commit_id",
        "completed_at",
        "consumed_at",
        "created_at",
        "expires_at",
        "grant_id",
        "grant_token_hash",
        "imported_event_id",
        "legacy_source_event_id",
        "proposal_id",
        "provenance",
        "provenance_json",
        "request_id",
        "run_id",
        "runtime_source_event_id",
        "source_event_id",
        "updated_at",
        "updated_order",
    }
)
PROPOSAL_BLOCK_VERSION = 1
PROPOSAL_OPEN_MARKER = "<plot-delta>"
PROPOSAL_CLOSE_MARKER = "</plot-delta>"
_PROPOSAL_RE = re.compile(
    re.escape(PROPOSAL_OPEN_MARKER)
    + r"\s*(\{.*?\})\s*"
    + re.escape(PROPOSAL_CLOSE_MARKER),
    re.DOTALL,
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]")
_MENTION_TO_ENTITY_FIELD = {
    "entity_mention": "entity_id",
    "actor_mention": "actor_entity_id",
    "location_mention": "location_entity_id",
    "to_location_mention": "to_location_entity_id",
    "item_mention": "item_entity_id",
    "owner_mention": "owner_entity_id",
    "to_owner_mention": "to_owner_entity_id",
    "from_owner_mention": "from_owner_entity_id",
    "source_mention": "source_entity_id",
    "target_mention": "target_entity_id",
    "believer_mention": "believer_entity_id",
    "ability_mention": "ability_entity_id",
}
_SIGNATURE_FIELDS = {
    "movement": (
        "event_type",
        "scope",
        "action",
        "actor_entity_id",
        "location_entity_id",
        "to_location_entity_id",
        "route",
    ),
    "inventory": (
        "event_type",
        "scope",
        "action",
        "item_entity_id",
        "from_owner_entity_id",
        "to_owner_entity_id",
        "quantity",
        "unique",
    ),
    "time": (
        "event_type",
        "scope",
        "field",
        "value",
        "story_time",
        "narrative_mode",
    ),
    "relation": (
        "event_type",
        "scope",
        "source_entity_id",
        "target_entity_id",
        "dimension",
        "value",
    ),
}
_FORBIDDEN_PRECOMPUTED_KEYS = {
    "dangerous_delta",
    "ingest_policy",
    "quarantined",
    "validation_status",
}


class BenchmarkCandidateError(ValueError):
    """A proposal candidate failed one explicit benchmark gate."""

    def __init__(
        self,
        stage: str,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.stage = stage
        self.code = code
        self.message = message
        self.details = dict(details or {})


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _is_exact_json_integer(value: Any) -> bool:
    """Return whether a decoded JSON value is an integer, excluding booleans."""

    return type(value) is int


def _normalized_surface(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", normalized.casefold().strip())


def _tokens(value: str) -> set[str]:
    return {match.group(0).casefold() for match in _TOKEN_RE.finditer(value)}


def _corpus_hash(records: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(_stable_json(record).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _is_volatile_power_projection_field(field_name: str) -> bool:
    normalized = str(field_name or "").casefold()
    return (
        normalized in _POWER_PROJECTION_VOLATILE_FIELDS
        or normalized.endswith("_approval_id")
        or normalized.endswith("_commit_id")
        or normalized.endswith("_event_id")
        or normalized.endswith("_proposal_id")
    )


def _normalize_power_projection_value(
    value: Any,
    *,
    field_name: str = "",
) -> Any:
    if _is_volatile_power_projection_field(field_name):
        return None
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_power_projection_value(
                item,
                field_name=str(key),
            )
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0]),
            )
            if not _is_volatile_power_projection_field(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [
            _normalize_power_projection_value(item)
            for item in value
        ]
    if isinstance(value, str) and field_name.casefold().endswith("_json"):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return value
        return _normalize_power_projection_value(decoded)
    return value


def _normalized_power_projection_hash(
    service: ContinuityService,
    power_tables: Iterable[str],
) -> str:
    """Hash semantic power projections without per-run provenance identifiers."""

    payload: dict[str, list[dict[str, Any]]] = {}
    with service.store.read_connection() as connection:
        for table in sorted({str(item) for item in power_tables}):
            columns = [
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                )
            ]
            semantic_columns = [
                column
                for column in columns
                if not _is_volatile_power_projection_field(column)
            ]
            if not semantic_columns:
                payload[table] = []
                continue
            projection_rows = connection.execute(
                f'SELECT * FROM "{table}"'
            ).fetchall()
            normalized_rows = [
                {
                    column: _normalize_power_projection_value(
                        row[column],
                        field_name=column,
                    )
                    for column in semantic_columns
                }
                for row in projection_rows
            ]
            payload[table] = sorted(
                normalized_rows,
                key=_stable_json,
            )
    digest = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    return f"power_projection_{digest}"


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        if _FORBIDDEN_PRECOMPUTED_KEYS & {
            _normalized_surface(key) for key in value
        }:
            return True
        return any(_contains_forbidden_key(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def load_annotation_manifest(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSONL at line {line_number}") from error
        if not isinstance(record, dict):
            raise ValueError(f"manifest line {line_number} must be an object")
        records.append(record)
    return records


def extract_proposal_candidates(
    assistant_text: str,
) -> list[dict[str, Any]]:
    """Parse proposal blocks from assistant output without consulting labels."""

    text = str(assistant_text or "")
    if text.count(PROPOSAL_OPEN_MARKER) != text.count(PROPOSAL_CLOSE_MARKER):
        raise ValueError("assistant_text has unmatched plot-delta markers")
    matches = list(_PROPOSAL_RE.finditer(text))
    if len(matches) != text.count(PROPOSAL_OPEN_MARKER):
        raise ValueError("assistant_text has an invalid plot-delta block")
    extracted: list[dict[str, Any]] = []
    for block_index, match in enumerate(matches, start=1):
        try:
            proposal = json.loads(match.group(1))
        except json.JSONDecodeError as error:
            raise ValueError(
                f"plot-delta block {block_index} is invalid JSON"
            ) from error
        if not isinstance(proposal, dict):
            raise ValueError(
                f"plot-delta block {block_index} must contain an object"
            )
        extracted.append(
            {
                "proposal": proposal,
                "block_span": (match.start(), match.end()),
            }
        )
    return extracted


def _catalog_index(
    catalog: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, Mapping[str, Any]]]:
    surfaces: dict[str, list[str]] = {}
    by_id: dict[str, Mapping[str, Any]] = {}
    for entity in catalog:
        entity_id = str(entity.get("entity_id") or "").strip()
        if not entity_id:
            continue
        by_id[entity_id] = entity
        names = [
            str(entity.get("canonical_name") or ""),
            *[str(alias) for alias in entity.get("aliases") or []],
        ]
        for name in names:
            normalized = _normalized_surface(name)
            if normalized:
                surfaces.setdefault(normalized, []).append(entity_id)
    return surfaces, by_id


def _validate_candidate_schema(proposal: Mapping[str, Any]) -> None:
    proposal_version = proposal.get("proposal_version")
    if (
        not _is_exact_json_integer(proposal_version)
        or proposal_version != PROPOSAL_BLOCK_VERSION
    ):
        raise BenchmarkCandidateError(
            "schema",
            "UNSUPPORTED_PROPOSAL_VERSION",
            "proposal_version is missing or unsupported",
        )
    candidate_id = str(proposal.get("candidate_id") or "").strip()
    if not candidate_id:
        raise BenchmarkCandidateError(
            "schema",
            "CANDIDATE_ID_REQUIRED",
            "candidate_id is required",
        )
    if not isinstance(proposal.get("event"), Mapping):
        raise BenchmarkCandidateError(
            "schema",
            "EVENT_OBJECT_REQUIRED",
            "event must be an object",
        )
    evidence = proposal.get("evidence")
    if not isinstance(evidence, Mapping):
        raise BenchmarkCandidateError(
            "schema",
            "EVIDENCE_OBJECT_REQUIRED",
            "evidence must be an object",
        )
    terms = proposal.get("evidence_terms")
    if (
        not isinstance(terms, list)
        or not terms
        or not all(isinstance(term, str) and term.strip() for term in terms)
    ):
        raise BenchmarkCandidateError(
            "schema",
            "EVIDENCE_TERMS_REQUIRED",
            "evidence_terms must contain at least one non-empty string",
        )
    if _contains_forbidden_key(proposal):
        raise BenchmarkCandidateError(
            "schema",
            "PRECOMPUTED_QUARANTINE_FORBIDDEN",
            "proposal may not contain a precomputed quarantine flag",
        )


def _resolve_entity_mentions(
    proposal: Mapping[str, Any],
    catalog: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    event = deepcopy(dict(proposal["event"]))
    surfaces, _by_id = _catalog_index(catalog)
    observations: list[dict[str, Any]] = []
    for mention_field, entity_field in _MENTION_TO_ENTITY_FIELD.items():
        if mention_field not in event:
            continue
        mention = str(event.get(mention_field) or "").strip()
        candidate_ids = list(surfaces.get(_normalized_surface(mention), []))
        observation = {
            "candidate_id": str(proposal["candidate_id"]),
            "mention_field": mention_field,
            "mention": mention,
            "entity_id": candidate_ids[0] if len(candidate_ids) == 1 else None,
            "candidate_entity_ids": candidate_ids,
        }
        observations.append(observation)
        if not candidate_ids:
            raise BenchmarkCandidateError(
                "entity_resolution",
                "ENTITY_MENTION_UNRESOLVED",
                f"entity mention is not in the case catalog: {mention!r}",
                details=observation,
            )
        if len(candidate_ids) != 1:
            raise BenchmarkCandidateError(
                "entity_resolution",
                "ENTITY_MENTION_AMBIGUOUS",
                f"entity mention resolves to multiple entities: {mention!r}",
                details=observation,
            )
        event[entity_field] = candidate_ids[0]
    event["evidence"] = dict(proposal["evidence"])
    return event, observations


def _validate_evidence(
    proposal: Mapping[str, Any],
    assistant_text: str,
    proposal_spans: Sequence[tuple[int, int]],
) -> None:
    evidence = dict(proposal["evidence"])
    quote = str(evidence.get("quote") or "")
    if not quote:
        raise BenchmarkCandidateError(
            "evidence",
            "EVIDENCE_QUOTE_REQUIRED",
            "evidence.quote is required",
        )
    start = evidence.get("start")
    end = evidence.get("end")
    if not _is_exact_json_integer(start) or not _is_exact_json_integer(end):
        raise BenchmarkCandidateError(
            "evidence",
            "EVIDENCE_SPAN_INVALID",
            "evidence start/end must be integers",
        )
    if start < 0 or end <= start or end > len(assistant_text):
        raise BenchmarkCandidateError(
            "evidence",
            "EVIDENCE_SPAN_INVALID",
            "evidence span is outside assistant_text",
        )
    if assistant_text[start:end] != quote:
        raise BenchmarkCandidateError(
            "evidence",
            "EVIDENCE_QUOTE_MISMATCH",
            "evidence quote does not equal the cited assistant_text span",
        )
    if any(start < block_end and end > block_start for block_start, block_end in proposal_spans):
        raise BenchmarkCandidateError(
            "evidence",
            "EVIDENCE_POINTS_TO_PROPOSAL",
            "evidence must cite generated prose, not the proposal block itself",
        )
    expected_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
    if str(evidence.get("sha256") or "") != expected_hash:
        raise BenchmarkCandidateError(
            "evidence",
            "EVIDENCE_HASH_MISMATCH",
            "evidence sha256 does not match the cited quote",
        )
    normalized_quote = _normalized_surface(quote)
    missing_terms = [
        term
        for term in proposal.get("evidence_terms") or []
        if _normalized_surface(term) not in normalized_quote
    ]
    if missing_terms:
        raise BenchmarkCandidateError(
            "evidence",
            "EVIDENCE_TERM_MISSING",
            "evidence quote does not support every declared event term",
            details={"missing_terms": missing_terms},
        )


def _validate_semantic_event(event: Mapping[str, Any]) -> None:
    event_type = str(event.get("event_type") or "")
    if (
        event_type == "relation"
        and event.get("source_entity_id") == event.get("target_entity_id")
    ):
        raise BenchmarkCandidateError(
            "semantic",
            "SELF_RELATION_NOT_ALLOWED",
            "a directed relationship delta requires distinct endpoints",
        )
    if event_type == "movement" and not (
        event.get("actor_entity_id")
        and (
            event.get("to_location_entity_id")
            or event.get("action") in {"leave", "depart"}
        )
    ):
        raise BenchmarkCandidateError(
            "semantic",
            "MOVEMENT_SEMANTICS_INCOMPLETE",
            "movement requires an actor and a valid location transition",
        )
    if event_type == "time" and not str(event.get("value") or "").strip():
        raise BenchmarkCandidateError(
            "semantic",
            "STORY_TIME_VALUE_REQUIRED",
            "story-time delta requires a concrete value",
        )


def event_signature(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return the semantic subset used for accepted-delta scoring."""

    event_type = str(event.get("event_type") or "")
    fields = _SIGNATURE_FIELDS.get(event_type)
    if fields is None:
        fields = tuple(
            sorted(
                key
                for key in event
                if key
                not in {
                    "branch_id",
                    "chapter_no",
                    "evidence",
                    "narrative_mode",
                    "scene_index",
                    "story_time",
                }
                and not key.endswith("_mention")
            )
        )
    return {
        field: event[field]
        for field in fields
        if field in event and event[field] is not None
    }


def evaluate_annotation_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Run one annotation from assistant output through the full gate."""

    assistant_text = str(record.get("assistant_text") or "")
    extracted = extract_proposal_candidates(assistant_text)
    proposal_spans = [tuple(item["block_span"]) for item in extracted]
    accepted: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    resolution_observations: list[dict[str, Any]] = []
    validator_invocations = {
        "schema": 0,
        "entity_resolution": 0,
        "evidence": 0,
        "continuity": 0,
        "semantic": 0,
    }
    seen_candidate_ids: set[str] = set()
    for item in extracted:
        proposal = dict(item["proposal"])
        candidate_id = str(proposal.get("candidate_id") or "")
        trace: list[str] = []
        try:
            validator_invocations["schema"] += 1
            trace.append("schema")
            _validate_candidate_schema(proposal)
            candidate_id = str(proposal["candidate_id"])
            if candidate_id in seen_candidate_ids:
                raise BenchmarkCandidateError(
                    "schema",
                    "DUPLICATE_CANDIDATE_ID",
                    f"duplicate candidate_id: {candidate_id}",
                )
            seen_candidate_ids.add(candidate_id)

            validator_invocations["entity_resolution"] += 1
            trace.append("entity_resolution")
            resolved_event, observations = _resolve_entity_mentions(
                proposal,
                list(record.get("entity_catalog") or []),
            )
            resolution_observations.extend(observations)

            validator_invocations["evidence"] += 1
            trace.append("evidence")
            _validate_evidence(proposal, assistant_text, proposal_spans)

            validator_invocations["continuity"] += 1
            trace.append("continuity")
            try:
                normalized_event = normalize_event(
                    resolved_event,
                    artifact_stage=str(record.get("artifact_stage") or "final"),
                    branch_id=str(record.get("branch_id") or "main"),
                    chapter_no=record.get("chapter_no"),
                    scene_index=record.get("scene_index"),
                )
            except ContinuityError as error:
                raise BenchmarkCandidateError(
                    "continuity",
                    error.code,
                    error.message,
                    details=error.details,
                ) from error

            validator_invocations["semantic"] += 1
            trace.append("semantic")
            _validate_semantic_event(normalized_event)
            accepted.append(
                {
                    "candidate_id": candidate_id,
                    "event": normalized_event,
                    "event_signature": event_signature(normalized_event),
                    "validator_trace": trace,
                }
            )
        except BenchmarkCandidateError as error:
            quarantined.append(
                {
                    "candidate_id": candidate_id,
                    "validator_stage": error.stage,
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                    "validator_trace": trace,
                }
            )
    return {
        "case_id": str(record.get("case_id") or ""),
        "proposal_candidate_count": len(extracted),
        "accepted": accepted,
        "quarantined": quarantined,
        "zero_delta": not accepted and not quarantined,
        "resolution_observations": resolution_observations,
        "validator_invocations": validator_invocations,
    }


def validate_annotation_manifest(
    path: str | Path,
    *,
    minimum_cases: int = 200,
) -> dict[str, Any]:
    records = load_annotation_manifest(path)
    if len(records) < minimum_cases:
        raise ValueError(
            f"benchmark manifest requires at least {minimum_cases} cases"
        )
    seen: set[str] = set()
    category_case_counts: dict[str, int] = {}
    category_positive_counts: dict[str, int] = {}
    zero_delta_case_count = 0
    quarantine_expected_count = 0
    continuity_quarantine_expected_count = 0
    semantic_quarantine_expected_count = 0
    resolution_annotation_count = 0
    alias_resolution_annotation_count = 0
    for index, record in enumerate(records, start=1):
        manifest_version = record.get("manifest_version")
        if (
            not _is_exact_json_integer(manifest_version)
            or manifest_version != BENCHMARK_MANIFEST_VERSION
        ):
            raise ValueError(f"case {index} has unsupported manifest version")
        case_id = str(record.get("case_id") or "")
        if not case_id or case_id in seen:
            raise ValueError(f"case {index} has duplicate or empty case_id")
        seen.add(case_id)
        assistant_text = str(record.get("assistant_text") or "")
        if not assistant_text.strip():
            raise ValueError(f"case {case_id} has empty assistant_text")
        try:
            extracted = extract_proposal_candidates(assistant_text)
        except ValueError as error:
            raise ValueError(f"case {case_id}: {error}") from error
        proposals = [dict(item["proposal"]) for item in extracted]
        if any(_contains_forbidden_key(proposal) for proposal in proposals):
            raise ValueError(
                f"case {case_id} contains a precomputed quarantine flag"
            )
        candidate_ids = [str(item.get("candidate_id") or "") for item in proposals]
        if "" in candidate_ids or len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError(f"case {case_id} candidate ids are invalid")

        expected_accepted = record.get("expected_accepted")
        expected_quarantine = record.get("expected_quarantine")
        if not isinstance(expected_accepted, list):
            raise ValueError(f"case {case_id} expected_accepted must be a list")
        if not isinstance(expected_quarantine, list):
            raise ValueError(f"case {case_id} expected_quarantine must be a list")
        accepted_ids: list[str] = []
        for expected in expected_accepted:
            if not isinstance(expected, Mapping):
                raise ValueError(
                    f"case {case_id} expected accepted item must be an object"
                )
            candidate_id = str(expected.get("candidate_id") or "")
            signature = expected.get("event_signature")
            if not candidate_id or not isinstance(signature, Mapping) or not signature:
                raise ValueError(
                    f"case {case_id} accepted label requires id and event_signature"
                )
            accepted_ids.append(candidate_id)
        quarantine_ids: list[str] = []
        for expected in expected_quarantine:
            if not isinstance(expected, Mapping):
                raise ValueError(
                    f"case {case_id} quarantine item must be an object"
                )
            candidate_id = str(expected.get("candidate_id") or "")
            stage = str(expected.get("validator_stage") or "")
            code = str(expected.get("code") or "")
            if (
                not candidate_id
                or stage
                not in {
                    "schema",
                    "entity_resolution",
                    "evidence",
                    "continuity",
                    "semantic",
                }
                or not code
            ):
                raise ValueError(
                    f"case {case_id} quarantine label requires id, stage, and code"
                )
            quarantine_ids.append(candidate_id)
            if stage == "continuity":
                continuity_quarantine_expected_count += 1
            if stage == "semantic":
                semantic_quarantine_expected_count += 1
        if set(accepted_ids) & set(quarantine_ids):
            raise ValueError(
                f"case {case_id} cannot accept and quarantine one candidate"
            )
        labeled_ids = set(accepted_ids) | set(quarantine_ids)
        if set(candidate_ids) != labeled_ids:
            raise ValueError(
                f"case {case_id} proposal ids do not match its outcome labels"
            )

        case_kind = str(record.get("case_kind") or "")
        if case_kind == "accepted_delta":
            if not accepted_ids or quarantine_ids:
                raise ValueError(f"case {case_id} has invalid accepted-delta labels")
        elif case_kind == "dangerous_delta":
            if accepted_ids or not quarantine_ids:
                raise ValueError(f"case {case_id} has invalid dangerous-delta labels")
        elif case_kind == "zero_delta":
            if candidate_ids or accepted_ids or quarantine_ids:
                raise ValueError(f"case {case_id} zero-delta must contain no proposal")
            zero_delta_case_count += 1
        else:
            raise ValueError(f"case {case_id} has unsupported case_kind")

        catalog = record.get("entity_catalog")
        if not isinstance(catalog, list):
            raise ValueError(f"case {case_id} entity_catalog must be a list")
        surfaces, by_id = _catalog_index(catalog)
        if any(len(entity_ids) != 1 for entity_ids in surfaces.values()):
            raise ValueError(f"case {case_id} entity catalog aliases are ambiguous")
        resolutions = record.get("expected_resolutions")
        if not isinstance(resolutions, list):
            raise ValueError(
                f"case {case_id} expected_resolutions must be a list"
            )
        for resolution in resolutions:
            if not isinstance(resolution, Mapping):
                raise ValueError(
                    f"case {case_id} resolution annotation must be an object"
                )
            candidate_id = str(resolution.get("candidate_id") or "")
            mention_field = str(resolution.get("mention_field") or "")
            mention = str(resolution.get("mention") or "")
            entity_id = str(resolution.get("entity_id") or "")
            if (
                candidate_id not in labeled_ids
                or mention_field not in _MENTION_TO_ENTITY_FIELD
                or entity_id not in by_id
                or surfaces.get(_normalized_surface(mention)) != [entity_id]
            ):
                raise ValueError(
                    f"case {case_id} has invalid entity-resolution annotation"
                )
            resolution_annotation_count += 1
            if bool(resolution.get("alias")):
                canonical_name = str(by_id[entity_id].get("canonical_name") or "")
                if _normalized_surface(mention) == _normalized_surface(canonical_name):
                    raise ValueError(
                        f"case {case_id} marks a canonical name as an alias"
                    )
                alias_resolution_annotation_count += 1

        category = str(record.get("category") or "unknown")
        category_case_counts[category] = category_case_counts.get(category, 0) + 1
        category_positive_counts[category] = (
            category_positive_counts.get(category, 0) + len(accepted_ids)
        )
        quarantine_expected_count += len(quarantine_ids)

    critical_categories = ("location", "inventory", "story_time", "relation")
    missing_critical = [
        category
        for category in critical_categories
        if category_positive_counts.get(category, 0) < 20
    ]
    if missing_critical:
        raise ValueError(
            "benchmark manifest requires at least 20 positive cases for "
            + ", ".join(missing_critical)
        )
    if zero_delta_case_count < 20:
        raise ValueError("benchmark manifest requires at least 20 zero-delta cases")
    if quarantine_expected_count < 20:
        raise ValueError(
            "benchmark manifest requires at least 20 dangerous deltas"
        )
    if continuity_quarantine_expected_count < 20:
        raise ValueError(
            "benchmark manifest requires at least 20 continuity-validator quarantines"
        )
    if alias_resolution_annotation_count < 40:
        raise ValueError(
            "benchmark manifest requires at least 40 alias-resolution annotations"
        )
    return {
        "manifest_version": BENCHMARK_MANIFEST_VERSION,
        "evaluation_mode": "assistant_text_proposal_gate",
        "case_count": len(records),
        "corpus_sha256": _corpus_hash(records),
        "category_case_counts": category_case_counts,
        "category_positive_counts": category_positive_counts,
        "zero_delta_case_count": zero_delta_case_count,
        "quarantine_expected_count": quarantine_expected_count,
        "continuity_quarantine_expected_count": (
            continuity_quarantine_expected_count
        ),
        "semantic_quarantine_expected_count": semantic_quarantine_expected_count,
        "entity_resolution_annotation_count": resolution_annotation_count,
        "alias_resolution_annotation_count": (
            alias_resolution_annotation_count
        ),
        "valid": True,
    }


def rank_labeled_candidates(
    query: str,
    candidates: Iterable[Mapping[str, Any]],
    *,
    limit: int = 1,
) -> tuple[list[str], int]:
    """Compatibility lexical ranker; quarantine is never fixture-supplied."""

    query_tokens = _tokens(query)
    ranked: list[tuple[float, int, str]] = []
    for candidate in candidates:
        candidate_id = str(candidate["id"])
        candidate_tokens = _tokens(str(candidate.get("text") or ""))
        coverage = len(query_tokens & candidate_tokens) / max(1, len(query_tokens))
        priority = int(candidate.get("priority", 0))
        ranked.append((coverage, priority, candidate_id))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    predicted = [
        candidate_id
        for score, _priority, candidate_id in ranked[: max(0, int(limit))]
        if score > 0
    ]
    return predicted, 0


def _accepted_label_key(candidate_id: str, signature: Mapping[str, Any]) -> str:
    return f"{candidate_id}:{_stable_json(dict(signature))}"


def _quarantine_label_key(candidate_id: str, stage: str, code: str) -> str:
    return f"{candidate_id}:{stage}:{code}"


def run_annotation_benchmark(
    path: str | Path,
    *,
    limit: int = 1,
) -> dict[str, Any]:
    validation = validate_annotation_manifest(path)
    records = load_annotation_manifest(path)
    true_positive = 0
    false_positive = 0
    false_negative = 0
    zero_result = 0
    quarantine_count = 0
    quarantine_true_positive = 0
    quarantine_false_positive = 0
    quarantine_false_negative = 0
    zero_delta_cases = 0
    zero_delta_correct = 0
    proposal_candidate_count = 0
    category_metrics: dict[str, dict[str, int]] = {}
    quarantine_reason_counts: dict[str, int] = {}
    quarantine_stage_counts: dict[str, int] = {}
    validator_invocations = {
        "schema": 0,
        "entity_resolution": 0,
        "evidence": 0,
        "continuity": 0,
        "semantic": 0,
    }
    resolution_total = 0
    resolution_correct = 0
    alias_resolution_total = 0
    alias_resolution_correct = 0

    for record in records:
        evaluation = evaluate_annotation_record(record)
        proposal_candidate_count += int(evaluation["proposal_candidate_count"])
        for stage, count in evaluation["validator_invocations"].items():
            validator_invocations[stage] += int(count)

        expected_accepted = {
            _accepted_label_key(
                str(item["candidate_id"]),
                dict(item["event_signature"]),
            )
            for item in record["expected_accepted"]
        }
        actual_accepted = {
            _accepted_label_key(
                str(item["candidate_id"]),
                dict(item["event_signature"]),
            )
            for item in evaluation["accepted"]
        }
        category = str(record.get("category") or "unknown")
        metrics = category_metrics.setdefault(
            category,
            {"tp": 0, "fp": 0, "fn": 0, "cases": 0, "positive": 0},
        )
        metrics["cases"] += 1
        metrics["positive"] += len(expected_accepted)
        case_tp = len(actual_accepted & expected_accepted)
        case_fp = len(actual_accepted - expected_accepted)
        case_fn = len(expected_accepted - actual_accepted)
        true_positive += case_tp
        false_positive += case_fp
        false_negative += case_fn
        metrics["tp"] += case_tp
        metrics["fp"] += case_fp
        metrics["fn"] += case_fn

        expected_quarantine = {
            _quarantine_label_key(
                str(item["candidate_id"]),
                str(item["validator_stage"]),
                str(item["code"]),
            )
            for item in record["expected_quarantine"]
        }
        actual_quarantine = {
            _quarantine_label_key(
                str(item["candidate_id"]),
                str(item["validator_stage"]),
                str(item["code"]),
            )
            for item in evaluation["quarantined"]
        }
        quarantine_count += len(actual_quarantine)
        quarantine_true_positive += len(expected_quarantine & actual_quarantine)
        quarantine_false_positive += len(actual_quarantine - expected_quarantine)
        quarantine_false_negative += len(expected_quarantine - actual_quarantine)
        for item in evaluation["quarantined"]:
            stage = str(item["validator_stage"])
            code = str(item["code"])
            quarantine_stage_counts[stage] = quarantine_stage_counts.get(stage, 0) + 1
            quarantine_reason_counts[code] = quarantine_reason_counts.get(code, 0) + 1

        if evaluation["zero_delta"]:
            zero_result += 1
        if str(record.get("case_kind")) == "zero_delta":
            zero_delta_cases += 1
            if evaluation["zero_delta"]:
                zero_delta_correct += 1

        actual_resolutions = {
            (
                str(item["candidate_id"]),
                str(item["mention_field"]),
            ): item
            for item in evaluation["resolution_observations"]
        }
        for expected in record["expected_resolutions"]:
            resolution_total += 1
            alias = bool(expected.get("alias"))
            if alias:
                alias_resolution_total += 1
            actual = actual_resolutions.get(
                (
                    str(expected["candidate_id"]),
                    str(expected["mention_field"]),
                )
            )
            correct = bool(
                actual
                and str(actual.get("mention")) == str(expected["mention"])
                and str(actual.get("entity_id")) == str(expected["entity_id"])
            )
            if correct:
                resolution_correct += 1
                if alias:
                    alias_resolution_correct += 1

    precision = true_positive / max(1, true_positive + false_positive)
    recall = true_positive / max(1, true_positive + false_negative)
    zero_delta_accuracy = zero_delta_correct / max(1, zero_delta_cases)
    quarantine_recall = quarantine_true_positive / max(
        1, quarantine_true_positive + quarantine_false_negative
    )
    quarantine_precision = quarantine_true_positive / max(
        1, quarantine_true_positive + quarantine_false_positive
    )
    resolution_accuracy = resolution_correct / max(1, resolution_total)
    alias_resolution_accuracy = alias_resolution_correct / max(
        1, alias_resolution_total
    )
    category_results: dict[str, dict[str, Any]] = {}
    for category, metrics in sorted(category_metrics.items()):
        category_recall = metrics["tp"] / max(1, metrics["tp"] + metrics["fn"])
        category_precision = metrics["tp"] / max(
            1, metrics["tp"] + metrics["fp"]
        )
        category_results[category] = {
            **metrics,
            "precision": round(category_precision, 8),
            "recall": round(category_recall, 8),
        }
    return {
        **validation,
        "tp": true_positive,
        "fp": false_positive,
        "fn": false_negative,
        "zero": zero_result,
        "quarantine": quarantine_count,
        "precision": round(precision, 8),
        "recall": round(recall, 8),
        "accepted_delta_precision": round(precision, 8),
        "accepted_delta_recall": round(recall, 8),
        "zero_delta_cases": zero_delta_cases,
        "zero_delta_correct": zero_delta_correct,
        "zero_delta_accuracy": round(zero_delta_accuracy, 8),
        "quarantine_tp": quarantine_true_positive,
        "quarantine_fp": quarantine_false_positive,
        "quarantine_fn": quarantine_false_negative,
        "quarantine_precision": round(quarantine_precision, 8),
        "quarantine_recall": round(quarantine_recall, 8),
        "quarantine_stage_counts": dict(sorted(quarantine_stage_counts.items())),
        "quarantine_reason_counts": dict(
            sorted(quarantine_reason_counts.items())
        ),
        "proposal_candidate_count": proposal_candidate_count,
        "validator_invocations": validator_invocations,
        "entity_resolution_total": resolution_total,
        "entity_resolution_correct": resolution_correct,
        "entity_resolution_accuracy": round(resolution_accuracy, 8),
        "alias_resolution_total": alias_resolution_total,
        "alias_resolution_correct": alias_resolution_correct,
        "alias_resolution_accuracy": round(alias_resolution_accuracy, 8),
        "category_metrics": category_results,
        "top_k": int(limit),
    }


def _validate_cross_system_power_case(
    record: Mapping[str, Any],
    delta: Mapping[str, Any],
    *,
    case_id: str,
    profile: str,
) -> None:
    """Require an executable no-rule cross-system conversion fixture."""

    if str(record.get("case_kind") or "") != "dangerous":
        raise ValueError(
            f"power case {case_id} cross-system coverage must be dangerous"
        )
    if (
        str(delta.get("event_type") or "") != "resource"
        or str(delta.get("action") or "") != "convert"
    ):
        raise ValueError(
            f"power case {case_id} cross-system coverage must be a resource conversion"
        )
    setup = record.get("power_setup")
    if not isinstance(setup, Mapping):
        raise ValueError(
            f"power case {case_id} must provide an executable power_setup"
        )
    required_setup_keys = {
        "systems",
        "resources",
        "bridge_rules",
        "conversion_rules",
    }
    if (
        set(setup) != required_setup_keys
        or setup.get("bridge_rules") != []
        or setup.get("conversion_rules") != []
    ):
        raise ValueError(
            f"power case {case_id} must define two systems/resources and "
            "explicitly declare empty bridge and conversion rules"
        )
    value = delta.get("value")
    if not isinstance(value, Mapping):
        raise ValueError(
            f"power case {case_id} cross-system conversion requires a value object"
        )
    source_resource = str(delta.get("object") or "")
    target_resource = str(value.get("target_resource") or "")
    conversion_rule = str(value.get("conversion_rule") or "")
    target_profiles = [
        candidate
        for candidate in POWER_BENCHMARK_PROFILES
        if candidate != profile
        and target_resource.startswith(
            f"{candidate}_目标体系资源_"
        )
    ]
    if (
        not source_resource.startswith(f"{profile}_源体系资源_")
        or not target_profiles
        or not conversion_rule
        or "没有任何桥接规则" not in str(
            record.get("assistant_text") or ""
        )
    ):
        raise ValueError(
            f"power case {case_id} does not prove a cross-system conversion with no accepted rule"
        )
    systems = setup.get("systems")
    resources = setup.get("resources")
    if (
        not isinstance(systems, list)
        or len(systems) != 2
        or not isinstance(resources, list)
        or len(resources) != 2
    ):
        raise ValueError(
            f"power case {case_id} must define exactly two systems and resources"
        )
    system_by_ref: dict[str, Mapping[str, Any]] = {}
    for system in systems:
        if not isinstance(system, Mapping):
            raise ValueError(
                f"power case {case_id} system setup must be an object"
            )
        ref = str(system.get("ref") or "").strip()
        system_profile = str(system.get("profile") or "").strip()
        if (
            ref not in {"source", "target"}
            or ref in system_by_ref
            or system_profile not in POWER_BENCHMARK_PROFILES
            or not str(system.get("name") or "").strip()
            or not str(system.get("namespace") or "").strip()
        ):
            raise ValueError(
                f"power case {case_id} contains an invalid system setup"
            )
        system_by_ref[ref] = system
    target_profile = target_profiles[0]
    if (
        set(system_by_ref) != {"source", "target"}
        or str(system_by_ref["source"].get("profile") or "") != profile
        or str(system_by_ref["target"].get("profile") or "")
        != target_profile
        or profile == target_profile
    ):
        raise ValueError(
            f"power case {case_id} must bind resources to distinct profiles"
        )
    resource_by_name: dict[str, Mapping[str, Any]] = {}
    for resource in resources:
        if not isinstance(resource, Mapping):
            raise ValueError(
                f"power case {case_id} resource setup must be an object"
            )
        name = str(resource.get("name") or "").strip()
        system_ref = str(resource.get("system_ref") or "").strip()
        if (
            not name
            or name in resource_by_name
            or system_ref not in system_by_ref
        ):
            raise ValueError(
                f"power case {case_id} contains an invalid resource setup"
            )
        resource_by_name[name] = resource
    if (
        set(resource_by_name) != {source_resource, target_resource}
        or str(resource_by_name[source_resource].get("system_ref") or "")
        != "source"
        or str(resource_by_name[target_resource].get("system_ref") or "")
        != "target"
    ):
        raise ValueError(
            f"power case {case_id} resources are not bound to opposite systems"
        )


def _install_power_benchmark_setup(
    service: ContinuityService,
    authority: HostApprovalAuthority,
    records: Sequence[Mapping[str, Any]],
    *,
    project_root: Path,
) -> dict[str, Any]:
    """Install accepted system/resource definitions without interaction rules."""

    system_entities: dict[tuple[str, str, str], str] = {}
    system_entities_by_profile: dict[str, str] = {}
    resource_entities: dict[tuple[str, str], str] = {}
    qualification_entities: dict[tuple[str, str], str] = {}
    setup_cases = 0
    definition_events: list[dict[str, Any]] = []
    for record in records:
        if "cross_system_dangerous" not in set(
            record.get("coverage_tags") or []
        ):
            continue
        setup_cases += 1
        setup = dict(record.get("power_setup") or {})
        local_systems: dict[str, str] = {}
        for raw_system in setup.get("systems") or []:
            system = dict(raw_system)
            name = str(system["name"])
            profile = str(system["profile"])
            namespace = str(system["namespace"])
            key = (name, profile, namespace)
            system_entity_id = system_entities.get(key)
            if system_entity_id is None:
                system_entity_id = str(
                    service.register_entity("power_system", name)["entity_id"]
                )
                system_entities[key] = system_entity_id
                definition_events.append(
                    {
                        "event_type": "power_spec",
                        "action": "define",
                        "spec_type": "power_system",
                        "spec_entity_id": system_entity_id,
                        "definition": {
                            "profile": profile,
                            "namespace": namespace,
                            "interaction_policy": "explicit_only",
                        },
                    }
                )
                system_entities_by_profile.setdefault(
                    profile,
                    system_entity_id,
                )
            local_systems[str(system["ref"])] = system_entity_id
        for raw_resource in setup.get("resources") or []:
            resource = dict(raw_resource)
            resource_name = str(resource["name"])
            system_entity_id = local_systems[str(resource["system_ref"])]
            key = (resource_name, system_entity_id)
            resource_entity_id = resource_entities.get(key)
            if resource_entity_id is None:
                resource_entity_id = str(
                    service.register_entity(
                        "resource_pool",
                        resource_name,
                    )["entity_id"]
                )
                resource_entities[key] = resource_entity_id
                definition_events.append(
                    {
                        "event_type": "power_spec",
                        "action": "define",
                        "spec_type": "resource_definition",
                        "spec_entity_id": resource_entity_id,
                        "definition": {
                            "system_entity_id": system_entity_id,
                            "allow_debt": False,
                        },
                    }
                )

    for record in records:
        if (
            str(record.get("case_kind") or "") != "accepted"
            or str(record.get("expected_event_type") or "")
            != "qualification"
        ):
            continue
        profile = str(record.get("profile") or "")
        system_entity_id = system_entities_by_profile.get(profile)
        if not system_entity_id:
            raise RuntimeError(
                "qualification benchmark setup has no accepted system "
                f"for profile {profile}"
            )
        for raw_delta in (
            (record.get("stop_envelope") or {}).get("deltas") or []
        ):
            delta = dict(raw_delta)
            if str(delta.get("event_type") or "") != "qualification":
                continue
            qualification_name = str(
                delta.get("object")
                or (delta.get("value") or {}).get("qualification")
                or delta.get("field")
                or ""
            ).strip()
            if not qualification_name:
                raise RuntimeError(
                    "qualification benchmark case has no qualification name: "
                    + str(record.get("case_id") or "")
                )
            key = (profile, qualification_name)
            if key in qualification_entities:
                continue
            qualification_entity_id = str(
                service.register_entity(
                    "qualification",
                    qualification_name,
                )["entity_id"]
            )
            qualification_entities[key] = qualification_entity_id
            definition_events.append(
                {
                    "event_type": "power_spec",
                    "action": "define",
                    "spec_type": "qualification_definition",
                    "spec_entity_id": qualification_entity_id,
                    "definition": {
                        "system_entity_id": system_entity_id,
                        "quantity_mode": "stackable",
                    },
                }
            )

    if not definition_events:
        return {
            "setup_cases": 0,
            "system_count": 0,
            "resource_count": 0,
            "qualification_definition_count": 0,
            "bridge_rule_count": 0,
            "conversion_rule_count": 0,
            "proposal_id": None,
            "commit_id": None,
        }
    revision = int(service.get_canon_revisions()["active"])
    proposal = service.save_proposal(
        events=definition_events,
        payload={
            "benchmark_suite": POWER_BENCHMARK_SUITE,
            "setup_kind": "cross_system_no_rules",
            "setup_cases": setup_cases,
        },
        artifact_id="power-benchmark:accepted-setup",
        artifact_stage="bootstrap",
        branch_id="main",
        prepared_canon_revision=revision,
        proposal_kind="power_spec_change",
        idempotency_key="power-benchmark:accepted-setup",
    )
    grant = authority.issue(
        str(proposal["proposal_id"]),
        expected_canon_revision=revision,
        operations=("accept_power_spec",),
        expires_in_seconds=300,
        target_project_real_path=project_root,
        authorized_paths=(),
    )
    commit = service.accept_proposal(
        str(proposal["proposal_id"]),
        approval_id=str(grant["approval_id"]),
        expected_canon_revision=revision,
    )
    with service.store.read_connection() as connection:
        bridge_rule_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM bridge_rules"
            ).fetchone()[0]
        )
        conversion_rule_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM conversion_rules"
            ).fetchone()[0]
        )
    if bridge_rule_count or conversion_rule_count:
        raise RuntimeError(
            "cross-system benchmark setup unexpectedly installed interaction rules"
        )
    return {
        "setup_cases": setup_cases,
        "system_count": len(system_entities),
        "resource_count": len(resource_entities),
        "qualification_definition_count": len(qualification_entities),
        "bridge_rule_count": bridge_rule_count,
        "conversion_rule_count": conversion_rule_count,
        "proposal_id": str(proposal["proposal_id"]),
        "commit_id": str(commit["commit_id"]),
        "active_canon_revision": int(commit["active_canon_revision"]),
    }


def _measure_mandatory_power_context_recall(
    project_root: Path,
) -> dict[str, Any]:
    """Run six production ContextContractBuilder power-task probes."""

    from .authority import AuthorityIndex, AuthoritySource
    from .continuity import ContextContractBuilder
    from .memory import LayeredMemoryStore

    probe_root = project_root / "mandatory-context-probe"
    setting_root = probe_root / "设定集"
    setting_root.mkdir(parents=True, exist_ok=True)
    prompts: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        (
            "combat",
            "战斗检索甲：推演战斗，核对能力、资源与冷却。",
            ("current_state", "power_state", "ability", "resource"),
        ),
        (
            "breakthrough",
            "突破检索乙：推演突破升级，核对境界、资源与失败结果。",
            ("current_state", "power_state", "progression", "resource"),
        ),
        (
            "training",
            "训练检索丙：推演训练领悟技能，核对前置和时间条件。",
            ("current_state", "power_state", "progression", "ability"),
        ),
        (
            "equipment",
            "装备检索丁：推演装备法器同调，核对绑定和能力。",
            ("current_state", "power_state", "ability", "power_binding"),
        ),
        (
            "system_reward",
            "系统检索戊：推演系统任务奖励，核对资源与权限。",
            ("current_state", "power_state", "resource"),
        ),
        (
            "contract",
            "契约检索己：推演契约召唤，核对绑定、资源和反噬。",
            ("current_state", "power_state", "resource", "power_binding"),
        ),
    )
    authority_marker = "POWER_CONTEXT_ACCEPTED_CANON"
    (setting_root / "力量上下文基准.md").write_text(
        authority_marker
        + "\n"
        + "\n".join(prompt for _, prompt, _ in prompts),
        encoding="utf-8",
    )
    index = AuthorityIndex(probe_root / ".plot-rag" / "authority.sqlite3")
    index.refresh(
        probe_root,
        [
            AuthoritySource(
                glob="设定集/**/*.md",
                role="setting",
                priority=100,
                scope_policy="infer_and_review",
                ingest_policy="include",
            )
        ],
    )
    memory = LayeredMemoryStore(
        probe_root / ".plot-rag" / "memory.sqlite3"
    )
    markers: dict[tuple[str, str], str] = {}
    for probe_id, prompt, categories in prompts:
        for category in categories:
            marker = f"POWER_CTX_{probe_id}_{category}"
            markers[(probe_id, category)] = marker
            memory.add(
                layer="working",
                category=category,
                content=f"{prompt} {marker} 已接受力量事实。",
                source_commit_id=f"context-probe:{probe_id}:{category}",
                canon_status="accepted",
                scope="current",
                metadata={
                    "benchmark_probe": probe_id,
                    "required_category": category,
                },
            )
    builder = ContextContractBuilder(index, memory_store=memory)
    expected = 0
    retrieved = 0
    failures: list[dict[str, Any]] = []
    contracts: list[dict[str, Any]] = []
    for probe_id, prompt, categories in prompts:
        contract = builder.build(
            prompt,
            task="prose",
            max_context_chars=5000,
            category_quotas={
                "accepted_authority": 1,
                "current_state": 1,
                "open_loop": 0,
                "power_state": 1,
                "progression": 1,
                "ability": 1,
                "resource": 1,
                "power_binding": 1,
            },
        )
        context_text = str(contract["context_text"])
        missing: list[str] = []
        for category in categories:
            expected += 1
            marker = markers[(probe_id, category)]
            if marker in context_text:
                retrieved += 1
            else:
                missing.append(category)
                failures.append(
                    {
                        "probe_id": probe_id,
                        "category": category,
                        "marker": marker,
                    }
                )
        contracts.append(
            {
                "probe_id": probe_id,
                "required_categories": list(categories),
                "missing_required_categories": missing,
                "accepted_authority_selected": int(
                    contract["accepted_authority_selected"]
                ),
                "within_budget": bool(contract["within_budget"]),
            }
        )
    recall = retrieved / max(1, expected)
    return {
        "probe_count": len(prompts),
        "required_fact_count": expected,
        "retrieved_fact_count": retrieved,
        "mandatory_context_recall": round(recall, 8),
        "failures": failures,
        "contracts": contracts,
    }


def _accept_power_benchmark_proposal(
    service: ContinuityService,
    authority: HostApprovalAuthority,
    *,
    events: Sequence[Mapping[str, Any]],
    artifact_id: str,
    project_root: Path,
    proposal_kind: str = "story_delta",
    operation: str = "accept",
) -> dict[str, Any]:
    revision = int(service.get_canon_revisions()["active"])
    proposal = service.save_proposal(
        events=[dict(event) for event in events],
        artifact_id=artifact_id,
        artifact_stage=(
            "bootstrap"
            if proposal_kind == "power_spec_change"
            else "final"
        ),
        branch_id="main",
        prepared_canon_revision=revision,
        proposal_kind=proposal_kind,
        idempotency_key=f"power-benchmark-probe:{artifact_id}",
    )
    grant = authority.issue(
        str(proposal["proposal_id"]),
        expected_canon_revision=revision,
        operations=(operation,),
        expires_in_seconds=300,
        target_project_real_path=project_root,
        authorized_paths=(),
    )
    return service.accept_proposal(
        str(proposal["proposal_id"]),
        approval_id=str(grant["approval_id"]),
        expected_canon_revision=revision,
    )


def _measure_ability_availability(
    service: ContinuityService,
    authority: HostApprovalAuthority,
    *,
    project_root: Path,
) -> dict[str, Any]:
    """Measure deterministic availability over ownership and cooldown states."""

    actor = str(
        service.register_entity(
            "character",
            "ability-availability-probe-actor",
        )["entity_id"]
    )
    system = str(
        service.register_entity(
            "power_system",
            "ability-availability-probe-system",
        )["entity_id"]
    )
    ability = str(
        service.register_entity(
            "ability",
            "ability-availability-probe",
        )["entity_id"]
    )
    _accept_power_benchmark_proposal(
        service,
        authority,
        events=[
            {
                "event_type": "power_spec",
                "action": "define",
                "spec_type": "power_system",
                "spec_entity_id": system,
                "definition": {
                    "profile": "magic",
                    "namespace": "benchmark:availability",
                },
            },
            {
                "event_type": "power_spec",
                "action": "define",
                "spec_type": "ability_definition",
                "spec_entity_id": ability,
                "definition": {
                    "system_entity_id": system,
                    "requirements": [],
                },
            },
        ],
        artifact_id="ability-availability-spec",
        project_root=project_root,
        proposal_kind="power_spec_change",
        operation="accept_power_spec",
    )

    def coordinate(ordinal: int) -> dict[str, Any]:
        return {
            "calendar_id": "availability-probe",
            "ordinal": ordinal,
            "label": f"能力判定时点{ordinal}",
            "precision": "scene",
        }

    observations: list[dict[str, Any]] = []

    def observe(label: str, expected: bool, ordinal: int) -> None:
        result = service.explain_power_action(
            actor,
            ability_id=ability,
            action="use",
            story_coordinate=coordinate(ordinal),
        )
        observations.append(
            {
                "label": label,
                "expected_executable": expected,
                "actual_executable": bool(result["executable"]),
                "reason_codes": [
                    str(reason.get("code") or "")
                    for reason in result.get("reasons") or []
                ],
            }
        )

    observe("before_gain", False, 0)
    _accept_power_benchmark_proposal(
        service,
        authority,
        events=[
            {
                "event_type": "ability",
                "owner_entity_id": actor,
                "ability_entity_id": ability,
                "action": "gain",
                "state": {"level": 1},
                "story_coordinate": coordinate(1),
            }
        ],
        artifact_id="ability-availability-gain",
        project_root=project_root,
    )
    observe("after_gain", True, 1)
    _accept_power_benchmark_proposal(
        service,
        authority,
        events=[
            {
                "event_type": "ability",
                "owner_entity_id": actor,
                "ability_entity_id": ability,
                "action": "use",
                "story_coordinate": coordinate(2),
                "cooldown_until": coordinate(4),
            }
        ],
        artifact_id="ability-availability-use",
        project_root=project_root,
    )
    observe("during_cooldown", False, 3)
    observe("after_cooldown", True, 4)
    _accept_power_benchmark_proposal(
        service,
        authority,
        events=[
            {
                "event_type": "ability",
                "owner_entity_id": actor,
                "ability_entity_id": ability,
                "action": "lose",
                "story_coordinate": coordinate(5),
            }
        ],
        artifact_id="ability-availability-lose",
        project_root=project_root,
    )
    observe("after_loss", False, 5)

    true_positive = sum(
        1
        for item in observations
        if item["expected_executable"] and item["actual_executable"]
    )
    false_positive = sum(
        1
        for item in observations
        if not item["expected_executable"] and item["actual_executable"]
    )
    false_negative = sum(
        1
        for item in observations
        if item["expected_executable"] and not item["actual_executable"]
    )
    correct = sum(
        1
        for item in observations
        if item["expected_executable"] == item["actual_executable"]
    )
    precision = true_positive / max(1, true_positive + false_positive)
    recall = true_positive / max(1, true_positive + false_negative)
    return {
        "probe_count": len(observations),
        "correct": correct,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "ability_availability_precision": round(precision, 8),
        "ability_availability_recall": round(recall, 8),
        "ability_availability_accuracy": round(
            correct / max(1, len(observations)),
            8,
        ),
        "observations": observations,
    }


def _measure_hidden_knowledge_boundary(
    service: ContinuityService,
    authority: HostApprovalAuthority,
    *,
    project_root: Path,
    public_query: Any,
) -> dict[str, Any]:
    """Prove author-plan observations stay out of default query planes."""

    observer = str(
        service.register_entity(
            "character",
            "hidden-knowledge-probe-observer",
        )["entity_id"]
    )
    subject = str(
        service.register_entity(
            "character",
            "hidden-knowledge-probe-subject",
        )["entity_id"]
    )
    ability = str(
        service.register_entity(
            "ability",
            "hidden-knowledge-probe-ability",
        )["entity_id"]
    )
    commit = _accept_power_benchmark_proposal(
        service,
        authority,
        events=[
            {
                "event_type": "power_observation",
                "observer_entity_id": observer,
                "subject_entity_id": subject,
                "ability_entity_id": ability,
                "action": "observe",
                "knowledge_plane": "author_plan",
                "confidence": 1.0,
                "observed_fields": {"hidden": True},
            }
        ],
        artifact_id="hidden-knowledge-boundary",
        project_root=project_root,
    )
    hidden_source_ids = {
        str(event["event_id"])
        for event in commit.get("events") or []
        if event.get("event_type") == "power_observation"
    }
    default_state = public_query(
        project_root,
        entity_id=subject,
        include_historical=False,
    )
    author_state = public_query(
        project_root,
        entity_id=subject,
        knowledge_planes=("author_plan",),
        include_historical=False,
    )
    default_ids = {
        str(item.get("source_event_id") or "")
        for item in default_state.get("observations") or []
    }
    author_ids = {
        str(item.get("source_event_id") or "")
        for item in author_state.get("observations") or []
    }
    leaked = sorted(hidden_source_ids & default_ids)
    explicitly_retrieved = sorted(hidden_source_ids & author_ids)
    ownership_leaks = [
        str(item.get("source_event_id") or "")
        for item in default_state.get("abilities") or []
        if str(item.get("ability_entity_id") or "") == ability
    ]
    return {
        "hidden_fact_count": len(hidden_source_ids),
        "hidden_knowledge_leaks": len(leaked) + len(ownership_leaks),
        "leaked_source_event_ids": leaked,
        "ownership_leak_source_event_ids": ownership_leaks,
        "explicit_author_plan_retrieval_count": len(explicitly_retrieved),
        "explicit_author_plan_source_event_ids": explicitly_retrieved,
    }


def validate_power_annotation_manifest(
    path: str | Path,
    *,
    minimum_cases: int = 360,
) -> dict[str, Any]:
    """Validate the typed Stop v3 power-system benchmark corpus."""

    manifest_path = Path(path)
    records = load_annotation_manifest(manifest_path)
    if len(records) < int(minimum_cases):
        raise ValueError(
            "power benchmark manifest requires at least "
            f"{int(minimum_cases)} cases"
        )

    seen_case_ids: set[str] = set()
    profile_case_counts: dict[str, int] = {}
    case_kind_counts = {
        "accepted": 0,
        "dangerous": 0,
        "zero_delta": 0,
    }
    event_family_counts: dict[str, dict[str, int]] = {
        family: {"accepted": 0, "dangerous": 0, "total": 0}
        for family in POWER_EVENT_FAMILIES
    }
    typed_stop_case_count = 0
    profile_probe_count = 0
    cross_system_dangerous_count = 0
    knowledge_boundary_count = 0
    replay_correction_retraction_count = 0

    expected_status_by_kind = {
        "accepted": "accepted",
        "dangerous": "quarantined",
        "zero_delta": "zero_delta",
    }
    for line_number, record in enumerate(records, start=1):
        manifest_version = record.get("manifest_version")
        if (
            not _is_exact_json_integer(manifest_version)
            or manifest_version != POWER_BENCHMARK_MANIFEST_VERSION
        ):
            raise ValueError(
                f"power case {line_number} has unsupported manifest version"
            )
        if str(record.get("suite") or "") != POWER_BENCHMARK_SUITE:
            raise ValueError(
                f"power case {line_number} has an unsupported suite"
            )
        case_id = str(record.get("case_id") or "").strip()
        if not case_id or case_id in seen_case_ids:
            raise ValueError(
                f"power case {line_number} has duplicate or empty case_id"
            )
        seen_case_ids.add(case_id)
        profile = str(record.get("profile") or "").strip()
        if profile not in POWER_BENCHMARK_PROFILES:
            raise ValueError(
                f"power case {case_id} has unsupported profile: {profile}"
            )
        if not str(record.get("profile_probe") or "").strip():
            raise ValueError(
                f"power case {case_id} requires a profile detection probe"
            )
        profile_probe_count += 1
        profile_case_counts[profile] = profile_case_counts.get(profile, 0) + 1

        case_kind = str(record.get("case_kind") or "").strip()
        if case_kind not in case_kind_counts:
            raise ValueError(
                f"power case {case_id} has unsupported case_kind"
            )
        case_kind_counts[case_kind] += 1
        if str(record.get("expected_status") or "") != (
            expected_status_by_kind[case_kind]
        ):
            raise ValueError(
                f"power case {case_id} expected_status disagrees with case_kind"
            )
        assistant_text = str(record.get("assistant_text") or "")
        if not assistant_text.strip():
            raise ValueError(
                f"power case {case_id} has empty assistant_text"
            )

        envelope = record.get("stop_envelope")
        if (
            not isinstance(envelope, Mapping)
            or set(envelope) != {"schema_version", "deltas"}
            or envelope.get("schema_version") != "plot-rag-delta/v3"
            or not isinstance(envelope.get("deltas"), list)
        ):
            raise ValueError(
                f"power case {case_id} must contain one typed Stop v3 envelope"
            )
        typed_stop_case_count += 1
        deltas = list(envelope["deltas"])
        expected_length = 0 if case_kind == "zero_delta" else 1
        if len(deltas) != expected_length:
            raise ValueError(
                f"power case {case_id} has an invalid delta count"
            )

        expected_event_type = record.get("expected_event_type")
        if case_kind == "zero_delta":
            if expected_event_type is not None:
                raise ValueError(
                    f"power case {case_id} zero-delta label must be null"
                )
        else:
            delta = deltas[0]
            if not isinstance(delta, Mapping):
                raise ValueError(
                    f"power case {case_id} delta must be an object"
                )
            event_type = str(delta.get("event_type") or "")
            if (
                event_type not in event_family_counts
                or event_type != str(expected_event_type or "")
            ):
                raise ValueError(
                    f"power case {case_id} has an invalid event-family label"
                )
            event_family_counts[event_type][case_kind] += 1
            event_family_counts[event_type]["total"] += 1

        coverage_tags = record.get("coverage_tags") or []
        if (
            not isinstance(coverage_tags, list)
            or any(
                not isinstance(tag, str) or not tag.strip()
                for tag in coverage_tags
            )
        ):
            raise ValueError(
                f"power case {case_id} coverage_tags must be strings"
            )
        tags = {str(tag).strip() for tag in coverage_tags}
        if "cross_system_dangerous" in tags:
            if not deltas or not isinstance(deltas[0], Mapping):
                raise ValueError(
                    f"power case {case_id} cross-system coverage requires one typed delta"
                )
            _validate_cross_system_power_case(
                record,
                deltas[0],
                case_id=case_id,
                profile=profile,
            )
            cross_system_dangerous_count += 1
        if "knowledge_boundary" in tags:
            knowledge_boundary_count += 1
        if tags & {"replay", "correction", "retraction"}:
            replay_correction_retraction_count += 1

    missing_profiles = sorted(
        set(POWER_BENCHMARK_PROFILES) - set(profile_case_counts)
    )
    if missing_profiles:
        raise ValueError(
            "power benchmark is missing profiles: "
            + ", ".join(missing_profiles)
        )
    underfilled_profiles = sorted(
        profile
        for profile in POWER_BENCHMARK_PROFILES
        if profile_case_counts.get(profile, 0) < 30
    )
    if underfilled_profiles:
        raise ValueError(
            "power benchmark requires at least 30 cases for: "
            + ", ".join(underfilled_profiles)
        )
    missing_families = sorted(
        family
        for family, counts in event_family_counts.items()
        if counts["total"] == 0
    )
    if missing_families:
        raise ValueError(
            "power benchmark is missing event families: "
            + ", ".join(missing_families)
        )
    if cross_system_dangerous_count < 60:
        raise ValueError(
            "power benchmark requires at least 60 cross-system dangerous cases"
        )
    if knowledge_boundary_count < 40:
        raise ValueError(
            "power benchmark requires at least 40 knowledge-boundary cases"
        )
    if replay_correction_retraction_count < 40:
        raise ValueError(
            "power benchmark requires at least 40 replay/correction/retraction cases"
        )
    return {
        "manifest_version": POWER_BENCHMARK_MANIFEST_VERSION,
        "suite": POWER_BENCHMARK_SUITE,
        "evaluation_mode": "typed_stop_v3_to_accepted_continuity",
        "case_count": len(records),
        "corpus_sha256": _corpus_hash(records),
        "manifest_file_sha256": hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
        "profile_count": len(profile_case_counts),
        "profile_case_counts": dict(sorted(profile_case_counts.items())),
        "case_kind_counts": case_kind_counts,
        "event_family_counts": event_family_counts,
        "typed_stop_case_count": typed_stop_case_count,
        "typed_stop_coverage": round(
            typed_stop_case_count / max(1, len(records)),
            8,
        ),
        "profile_probe_count": profile_probe_count,
        "cross_system_dangerous_count": cross_system_dangerous_count,
        "knowledge_boundary_count": knowledge_boundary_count,
        "replay_correction_retraction_count": (
            replay_correction_retraction_count
        ),
        "valid": True,
    }


def _power_benchmark_project() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory(
        prefix="plot-rag-power-benchmark-"
    )
    project_root = Path(temporary.name)
    config_dir = project_root / ".plot-rag"
    config_dir.mkdir(parents=True, exist_ok=True)
    template = (
        Path(__file__).resolve().parents[2]
        / "templates"
        / "config.v3.json"
    )
    if not template.is_file():
        temporary.cleanup()
        raise FileNotFoundError(
            f"checked-in config template is missing: {template}"
        )
    (config_dir / "config.json").write_bytes(template.read_bytes())
    return temporary, project_root


def run_power_annotation_benchmark(
    path: str | Path,
) -> dict[str, Any]:
    """Exercise typed Stop, adapter, proposal, grant, accept, and replay."""

    validation = validate_power_annotation_manifest(path)
    records = load_annotation_manifest(path)
    cross_system_case_ids = {
        str(record.get("case_id") or "")
        for record in records
        if "cross_system_dangerous"
        in set(record.get("coverage_tags") or [])
    }

    # Delayed imports avoid a longform -> v1_runtime import cycle.
    import state_rag
    from power_system.adapters import detect_power_profile
    from v1_runtime import (
        legacy_deltas_to_events,
        query_power_state as public_query_power_state,
    )

    temporary, project_root = _power_benchmark_project()
    try:
        runtime = state_rag._load_runtime_config(project_root)
        service = ContinuityService(project_root)
        authority = HostApprovalAuthority(
            service,
            issuer="plot-rag-power-benchmark",
            channel="local_benchmark",
        )
        cross_system_setup = _install_power_benchmark_setup(
            service,
            authority,
            records,
            project_root=project_root,
        )
        expected_accepted = int(
            validation["case_kind_counts"]["accepted"]
        )
        expected_dangerous = int(
            validation["case_kind_counts"]["dangerous"]
        )
        expected_zero = int(
            validation["case_kind_counts"]["zero_delta"]
        )
        accepted_true_positive = 0
        accepted_false_positive = 0
        accepted_false_negative = 0
        quarantine_true_positive = 0
        quarantine_false_positive = 0
        quarantine_false_negative = 0
        zero_delta_correct = 0
        proposal_count = 0
        accepted_commit_count = 0
        extracted_delta_count = 0
        normalized_event_count = 0
        typed_stop_case_count = 0
        cross_system_interaction_block_count = 0
        cross_system_unbridged_accepts = 0
        profile_mapping_total = 0
        profile_mapping_correct = 0
        profile_mapping_failures: list[dict[str, str]] = []
        dangerous_block_stage_counts: dict[str, int] = {}
        dangerous_block_reason_counts: dict[str, int] = {}
        dangerous_blocks: list[dict[str, Any]] = []
        failed_cases: list[dict[str, Any]] = []
        profile_metrics: dict[str, dict[str, int]] = {
            profile: {
                "cases": 0,
                "accepted_expected": 0,
                "dangerous_expected": 0,
                "zero_expected": 0,
                "accepted": 0,
                "blocked": 0,
                "zero_correct": 0,
            }
            for profile in POWER_BENCHMARK_PROFILES
        }
        event_family_metrics: dict[str, dict[str, int]] = {
            family: {
                "cases": 0,
                "accepted_expected": 0,
                "dangerous_expected": 0,
                "extracted": 0,
                "normalized": 0,
                "accepted": 0,
                "blocked": 0,
            }
            for family in POWER_EVENT_FAMILIES
        }

        def record_block(
            *,
            case_id: str,
            profile: str,
            event_family: str | None,
            case_kind: str,
            stage: str,
            code: str,
            message: str,
        ) -> None:
            nonlocal quarantine_true_positive
            nonlocal quarantine_false_positive
            nonlocal cross_system_interaction_block_count
            block = {
                "case_id": case_id,
                "profile": profile,
                "event_family": event_family,
                "stage": stage,
                "code": code,
                "message": message,
            }
            if case_kind == "dangerous":
                dangerous_block_stage_counts[stage] = (
                    dangerous_block_stage_counts.get(stage, 0) + 1
                )
                dangerous_block_reason_counts[code] = (
                    dangerous_block_reason_counts.get(code, 0) + 1
                )
                if (
                    case_id in cross_system_case_ids
                    and code == "POWER_INTERACTION_UNKNOWN"
                ):
                    cross_system_interaction_block_count += 1
                quarantine_true_positive += 1
                dangerous_blocks.append(block)
                profile_metrics[profile]["blocked"] += 1
                if event_family:
                    event_family_metrics[event_family]["blocked"] += 1
            else:
                quarantine_false_positive += 1
                failed_cases.append(block)

        for case_index, record in enumerate(records, start=1):
            case_id = str(record["case_id"])
            case_kind = str(record["case_kind"])
            profile = str(record["profile"])
            detected_profile = detect_power_profile(
                str(record["profile_probe"])
            )
            profile_mapping_total += 1
            if detected_profile == profile:
                profile_mapping_correct += 1
            else:
                profile_mapping_failures.append(
                    {
                        "case_id": case_id,
                        "expected_profile": profile,
                        "detected_profile": detected_profile,
                    }
                )
            expected_event_type = record.get("expected_event_type")
            event_family = (
                str(expected_event_type)
                if expected_event_type is not None
                else None
            )
            profile_metrics[profile]["cases"] += 1
            if case_kind == "accepted":
                profile_metrics[profile]["accepted_expected"] += 1
            elif case_kind == "dangerous":
                profile_metrics[profile]["dangerous_expected"] += 1
            else:
                profile_metrics[profile]["zero_expected"] += 1
            if event_family:
                family_metrics = event_family_metrics[event_family]
                family_metrics["cases"] += 1
                family_metrics[f"{case_kind}_expected"] += 1

            envelope = dict(record["stop_envelope"])
            if envelope.get("schema_version") == "plot-rag-delta/v3":
                typed_stop_case_count += 1
            assistant_text = str(record["assistant_text"])
            try:
                deltas, skipped = state_rag._validate_deltas(
                    envelope,
                    assistant_text,
                    runtime,
                    "",
                )
            except Exception as error:
                record_block(
                    case_id=case_id,
                    profile=profile,
                    event_family=event_family,
                    case_kind=case_kind,
                    stage="extraction",
                    code=str(
                        getattr(error, "code", type(error).__name__)
                    ),
                    message=str(error),
                )
                continue

            extracted_delta_count += len(deltas)
            if event_family:
                event_family_metrics[event_family]["extracted"] += len(deltas)
            if skipped:
                record_block(
                    case_id=case_id,
                    profile=profile,
                    event_family=event_family,
                    case_kind=case_kind,
                    stage="extraction",
                    code="EXTRACTION_DELTA_SKIPPED",
                    message=_stable_json(skipped),
                )
                continue
            if case_kind == "zero_delta":
                if not deltas:
                    zero_delta_correct += 1
                    profile_metrics[profile]["zero_correct"] += 1
                else:
                    failed_cases.append(
                        {
                            "case_id": case_id,
                            "profile": profile,
                            "stage": "extraction",
                            "code": "ZERO_DELTA_FALSE_POSITIVE",
                            "message": "zero-delta case produced a delta",
                        }
                    )
                continue

            current_revision = service.get_canon_revisions()["active"]
            artifact_context = {
                "artifact_id": f"power-benchmark:{case_id}",
                "artifact_stage": "final",
                "branch_id": "main",
                "chapter_no": case_index,
                "scene_index": 0,
            }
            try:
                events, issues = legacy_deltas_to_events(
                    service,
                    deltas,
                    artifact_context=artifact_context,
                    receipt_id=f"power-benchmark:{case_id}",
                    assistant_hash=hashlib.sha256(
                        assistant_text.encode("utf-8")
                    ).hexdigest(),
                )
            except Exception as error:
                record_block(
                    case_id=case_id,
                    profile=profile,
                    event_family=event_family,
                    case_kind=case_kind,
                    stage="normalizer",
                    code=str(
                        getattr(error, "code", type(error).__name__)
                    ),
                    message=str(error),
                )
                continue

            normalized_event_count += len(events)
            if event_family:
                event_family_metrics[event_family]["normalized"] += len(
                    events
                )
            blocking_issues = [
                dict(issue)
                for issue in issues
                if str(issue.get("severity") or "").casefold()
                in {"error", "critical"}
            ]
            if not events or blocking_issues:
                record_block(
                    case_id=case_id,
                    profile=profile,
                    event_family=event_family,
                    case_kind=case_kind,
                    stage="normalizer",
                    code=(
                        str(blocking_issues[0].get("code"))
                        if blocking_issues
                        else "NO_NORMALIZED_EVENT"
                    ),
                    message=(
                        str(blocking_issues[0].get("message") or "")
                        if blocking_issues
                        else "typed delta produced no continuity event"
                    ),
                )
                continue

            proposal = service.save_proposal(
                events=events,
                payload={
                    "benchmark_suite": POWER_BENCHMARK_SUITE,
                    "benchmark_case_id": case_id,
                    "assistant_text": assistant_text,
                    "stop_envelope": envelope,
                },
                artifact_id=str(artifact_context["artifact_id"]),
                artifact_stage="final",
                branch_id="main",
                chapter_no=case_index,
                scene_index=0,
                prepared_canon_revision=current_revision,
                issues=issues,
                proposal_kind="story_delta",
                idempotency_key=f"power-benchmark-save:{case_id}",
            )
            proposal_count += 1
            if proposal.get("validation_status") != "valid":
                record_block(
                    case_id=case_id,
                    profile=profile,
                    event_family=event_family,
                    case_kind=case_kind,
                    stage="proposal",
                    code="PROPOSAL_QUARANTINED",
                    message=str(proposal.get("status_reason") or ""),
                )
                continue

            try:
                grant = authority.issue(
                    str(proposal["proposal_id"]),
                    expected_canon_revision=current_revision,
                    operations=("accept",),
                    expires_in_seconds=300,
                    target_project_real_path=project_root,
                    authorized_paths=(),
                )
                commit = service.accept_proposal(
                    str(proposal["proposal_id"]),
                    approval_id=str(grant["approval_id"]),
                    expected_canon_revision=current_revision,
                )
            except ContinuityError as error:
                record_block(
                    case_id=case_id,
                    profile=profile,
                    event_family=event_family,
                    case_kind=case_kind,
                    stage="accept_invariant",
                    code=error.code,
                    message=error.message,
                )
                continue

            accepted_commit_count += 1
            profile_metrics[profile]["accepted"] += 1
            if event_family:
                event_family_metrics[event_family]["accepted"] += 1
            if case_kind == "accepted":
                accepted_true_positive += 1
            else:
                accepted_false_positive += 1
                quarantine_false_negative += 1
                if case_id in cross_system_case_ids:
                    cross_system_unbridged_accepts += 1
                failed_cases.append(
                    {
                        "case_id": case_id,
                        "profile": profile,
                        "event_family": event_family,
                        "stage": "accept",
                        "code": "DANGEROUS_DELTA_ACCEPTED",
                        "message": (
                            "dangerous case reached accepted commit "
                            + str(commit.get("commit_id") or "")
                        ),
                    }
                )

        accepted_false_negative += max(
            0,
            expected_accepted - accepted_true_positive,
        )
        quarantine_false_negative += max(
            0,
            expected_dangerous
            - quarantine_true_positive
            - quarantine_false_negative,
        )
        accepted_precision = accepted_true_positive / max(
            1,
            accepted_true_positive + accepted_false_positive,
        )
        accepted_recall = accepted_true_positive / max(
            1,
            accepted_true_positive + accepted_false_negative,
        )
        quarantine_precision = quarantine_true_positive / max(
            1,
            quarantine_true_positive + quarantine_false_positive,
        )
        quarantine_recall = quarantine_true_positive / max(
            1,
            quarantine_true_positive + quarantine_false_negative,
        )
        zero_delta_accuracy = zero_delta_correct / max(1, expected_zero)
        profile_mapping_accuracy = profile_mapping_correct / max(
            1,
            profile_mapping_total,
        )

        mandatory_context = _measure_mandatory_power_context_recall(
            project_root
        )
        availability = _measure_ability_availability(
            service,
            authority,
            project_root=project_root,
        )
        hidden_knowledge = _measure_hidden_knowledge_boundary(
            service,
            authority,
            project_root=project_root,
            public_query=public_query_power_state,
        )
        expected_actor_belief_observations = sum(
            1
            for record in records
            if str(record.get("case_kind") or "") == "accepted"
            for delta in (
                (record.get("stop_envelope") or {}).get("deltas") or []
            )
            if str(delta.get("event_type") or "") == "power_observation"
            and str(delta.get("knowledge_plane") or "")
            == "actor_belief"
        )
        with service.store.read_connection() as connection:
            actor_belief_observations = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM power_observations
                    WHERE knowledge_plane='actor_belief'
                    """
                ).fetchone()[0]
            )
        knowledge_plane_preservation_accuracy = (
            min(
                actor_belief_observations,
                expected_actor_belief_observations,
            )
            / max(
                1,
                actor_belief_observations,
                expected_actor_belief_observations,
            )
        )

        projection_hash_before_replay = service.projection_hash()
        normalized_projection_hash_before_replay = (
            _normalized_power_projection_hash(
                service,
                state_rag.CONTINUITY_POWER_TABLES,
            )
        )
        first_replay = service.replay()
        projection_hash_after_first_replay = str(
            first_replay.get("projection_hash") or service.projection_hash()
        )
        normalized_projection_hash_after_first_replay = (
            _normalized_power_projection_hash(
                service,
                state_rag.CONTINUITY_POWER_TABLES,
            )
        )
        second_replay = service.replay()
        projection_hash_after_second_replay = str(
            second_replay.get("projection_hash") or service.projection_hash()
        )
        normalized_projection_hash_after_second_replay = (
            _normalized_power_projection_hash(
                service,
                state_rag.CONTINUITY_POWER_TABLES,
            )
        )
        replay_hash_stable = (
            projection_hash_before_replay
            == projection_hash_after_first_replay
            == projection_hash_after_second_replay
        )
        normalized_projection_hash_stable = (
            normalized_projection_hash_before_replay
            == normalized_projection_hash_after_first_replay
            == normalized_projection_hash_after_second_replay
        )
        typed_stop_coverage = typed_stop_case_count / max(1, len(records))
        passed = (
            accepted_precision >= 0.99
            and accepted_recall >= 0.95
            and quarantine_recall == 1.0
            and zero_delta_accuracy >= 0.98
            and typed_stop_coverage == 1.0
            and profile_mapping_accuracy >= 0.97
            and mandatory_context["mandatory_context_recall"] >= 0.98
            and availability["ability_availability_precision"] >= 0.99
            and availability["ability_availability_accuracy"] == 1.0
            and hidden_knowledge["hidden_knowledge_leaks"] == 0
            and hidden_knowledge["explicit_author_plan_retrieval_count"]
            == hidden_knowledge["hidden_fact_count"]
            and actor_belief_observations
            == expected_actor_belief_observations
            and knowledge_plane_preservation_accuracy == 1.0
            and replay_hash_stable
            and normalized_projection_hash_stable
            and validation["cross_system_dangerous_count"] >= 60
            and cross_system_setup["setup_cases"]
            == validation["cross_system_dangerous_count"]
            and cross_system_setup["system_count"]
            >= len(POWER_BENCHMARK_PROFILES)
            and cross_system_setup["bridge_rule_count"] == 0
            and cross_system_setup["conversion_rule_count"] == 0
            and cross_system_unbridged_accepts == 0
            and cross_system_interaction_block_count
            == validation["cross_system_dangerous_count"]
            and validation["knowledge_boundary_count"] >= 40
            and validation["replay_correction_retraction_count"] >= 40
        )
        return {
            **validation,
            "status": "passed" if passed else "failed",
            "passed": passed,
            "proposal_count": proposal_count,
            "accepted_commit_count": accepted_commit_count,
            "extracted_delta_count": extracted_delta_count,
            "normalized_event_count": normalized_event_count,
            "accepted_tp": accepted_true_positive,
            "accepted_fp": accepted_false_positive,
            "accepted_fn": accepted_false_negative,
            "accepted_delta_precision": round(accepted_precision, 8),
            "accepted_delta_recall": round(accepted_recall, 8),
            "quarantine_tp": quarantine_true_positive,
            "quarantine_fp": quarantine_false_positive,
            "quarantine_fn": quarantine_false_negative,
            "quarantine_precision": round(quarantine_precision, 8),
            "quarantine_recall": round(quarantine_recall, 8),
            "zero_delta_cases": expected_zero,
            "zero_delta_correct": zero_delta_correct,
            "zero_delta_accuracy": round(zero_delta_accuracy, 8),
            "typed_stop_coverage": round(typed_stop_coverage, 8),
            "profile_mapping_total": profile_mapping_total,
            "profile_mapping_correct": profile_mapping_correct,
            "profile_mapping_accuracy": round(
                profile_mapping_accuracy,
                8,
            ),
            "profile_mapping_failures": profile_mapping_failures,
            "mandatory_context": mandatory_context,
            "mandatory_context_recall": mandatory_context[
                "mandatory_context_recall"
            ],
            "ability_availability": availability,
            "ability_availability_precision": availability[
                "ability_availability_precision"
            ],
            "hidden_knowledge": hidden_knowledge,
            "hidden_knowledge_leaks": hidden_knowledge[
                "hidden_knowledge_leaks"
            ],
            "knowledge_plane_preservation": {
                "expected_actor_belief_observations": (
                    expected_actor_belief_observations
                ),
                "projected_actor_belief_observations": (
                    actor_belief_observations
                ),
                "accuracy": round(
                    knowledge_plane_preservation_accuracy,
                    8,
                ),
            },
            "metric_sources": {
                "profile_mapping_accuracy": (
                    "360 fixture profile_probe values through "
                    "power_system.adapters.detect_power_profile"
                ),
                "mandatory_context_recall": (
                    "6 production ContextContractBuilder task probes "
                    "covering combat, breakthrough, training, equipment, "
                    "system reward, and contract/召唤"
                ),
                "ability_availability_precision": (
                    "5 deterministic ContinuityService.explain_power_action "
                    "ownership/cooldown probes"
                ),
                "hidden_knowledge_leaks": (
                    "author_plan accepted observation queried through the "
                    "public default and explicit knowledge-plane paths"
                ),
                "cross_system_unbridged_accepts": (
                    "60 typed Stop conversions between accepted distinct "
                    "PowerSystemSpec/ResourceDefinition pairs with zero "
                    "BridgeRule and ConversionRule rows"
                ),
                "adapter_runtime_contract": (
                    "tests/test_power_adapter_runtime_contract.py runs every "
                    "declared profile through the shared ContinuityService"
                ),
                "normalized_projection_hash": (
                    "all state_rag.CONTINUITY_POWER_TABLES rows after "
                    "removing per-run approval/commit/event provenance"
                ),
            },
            "cross_system_interaction_block_count": (
                cross_system_interaction_block_count
            ),
            "cross_system_unbridged_accepts": (
                cross_system_unbridged_accepts
            ),
            "cross_system_setup": cross_system_setup,
            "profile_metrics": profile_metrics,
            "event_family_metrics": event_family_metrics,
            "dangerous_block_stage_counts": dict(
                sorted(dangerous_block_stage_counts.items())
            ),
            "dangerous_block_reason_counts": dict(
                sorted(dangerous_block_reason_counts.items())
            ),
            "dangerous_blocks": dangerous_blocks,
            "failed_cases": failed_cases,
            "replay": {
                "projection_hash_before": projection_hash_before_replay,
                "projection_hash_after_first": (
                    projection_hash_after_first_replay
                ),
                "projection_hash_after_second": (
                    projection_hash_after_second_replay
                ),
                "hash_stable": replay_hash_stable,
                "normalized_projection_hash_before": (
                    normalized_projection_hash_before_replay
                ),
                "normalized_projection_hash_after_first": (
                    normalized_projection_hash_after_first_replay
                ),
                "normalized_projection_hash_after_second": (
                    normalized_projection_hash_after_second_replay
                ),
                "normalized_hash_stable": (
                    normalized_projection_hash_stable
                ),
                "first": first_replay,
                "second": second_replay,
            },
            "quality_gate": {
                "accepted_delta_precision_min": 0.99,
                "accepted_delta_recall_min": 0.95,
                "quarantine_recall_required": 1.0,
                "zero_delta_accuracy_min": 0.98,
                "typed_stop_coverage_required": 1.0,
                "profile_mapping_accuracy_min": 0.97,
                "mandatory_context_recall_min": 0.98,
                "ability_availability_precision_min": 0.99,
                "hidden_knowledge_leaks_required": 0,
                "knowledge_plane_preservation_required": 1.0,
                "replay_hash_stable_required": True,
                "normalized_replay_hash_stable_required": True,
                "cross_system_interaction_blocks_required": validation[
                    "cross_system_dangerous_count"
                ],
                "cross_system_setup_required": True,
                "cross_system_unbridged_accepts_required": 0,
                "passed": passed,
            },
        }
    finally:
        temporary.cleanup()
