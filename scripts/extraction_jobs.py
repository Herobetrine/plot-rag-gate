"""Durable asynchronous extraction-job queue.

The queue is deliberately a local control-plane component.  Enqueueing a job
only persists immutable extraction inputs; it never performs an HTTP request
or invokes a model.  A worker must claim a fenced lease before it may update a
job.  The lease's ``attempt_count`` is the fencing epoch, so a stale worker
cannot complete a job after lease recovery and a later claim.

The canonical lifecycle remains outside this module.  A successful worker may
bind a durable proposal ID, but it cannot accept that proposal here.  The
``barrier_status`` method joins the proposal table and keeps later story work
blocked until the proposal has an explicit terminal disposition.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__:  # Package import used by tests and ``python -m`` entry points.
    from .continuity.store import ContinuityStore
else:  # Direct ``python scripts/<cli>.py`` or top-level import mode.
    from continuity.store import ContinuityStore


JOB_STATUSES = frozenset(
    {"queued", "running", "succeeded", "failed", "cancelled"}
)
TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
BARRIER_CODES = frozenset(
    {
        "clear",
        "queued",
        "running",
        "failed",
        "pending_review",
        "accepted",
        "rejected",
        "retracted",
        "cancelled",
    }
)
BARRIER_RESOLUTION_ACTIONS = frozenset(
    {"discard", "rewrite", "supersede", "branch_switch"}
)
RESULT_KINDS = frozenset({"proposal", "no_delta"})

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_STABLE_HASH_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9_]*_)?[0-9a-fA-F]{64}$"
)
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_LIST_LIMIT = 1000
MAX_ASSISTANT_PAYLOAD_BYTES = 16 * 1024 * 1024
_SECRET_RE = re.compile(
    r"""(?ix)
    (?:
        \bbearer\s+["']?[^\s"',;|]{8,}["']?
        |
        \b(?:api[_-]?key|authorization|password|passwd|secret|
              access[_-]?token|refresh[_-]?token|credential|
              client[_-]?secret|token|cookie|set[_-]?cookie)\b
        \s*["']?\s*[:=]\s*["']?
        (?:bearer\s+)?[^\s"',;|]{8,}["']?
        |
        \b(?:sk|sf|ak)-[A-Za-z0-9._~+/=-]{8,}
    )
    """
)
_SECRET_ENV_NAME_RE = re.compile(
    r"""(?ix)
    (?:^|_)
    (?:
        api[_-]?key|key|token|secret|password|passwd|credential|
        authorization|cookie
    )
    (?:$|_)
    """
)


class ExtractionJobError(RuntimeError):
    """Base error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(f"{self.code}: {message}")


class ExtractionJobNotFound(ExtractionJobError):
    """Raised when a requested job does not exist."""


class ExtractionJobConflict(ExtractionJobError):
    """Raised for idempotency or compare-and-swap conflicts."""


class ExtractionLeaseLost(ExtractionJobConflict):
    """Raised when a worker no longer owns the current lease epoch."""


@dataclass(frozen=True)
class ExtractionWorkResult:
    """Validated result returned by a ``run_once`` proposal factory."""

    validator_passed: bool
    result_proposal_id: str | None = None
    result_kind: str | None = None
    remote_status: str = "validated"
    error: str = ""


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExtractionJobError(
            "EXTRACTION_JOB_INVALID_ARGUMENT",
            f"{name} must be a non-empty string",
            details={"field": name},
        )
    return value.strip()


def _require_hash(
    value: Any,
    name: str,
    *,
    allow_empty: bool = False,
) -> str:
    if allow_empty and (value is None or value == ""):
        return ""
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ExtractionJobError(
            "EXTRACTION_JOB_INVALID_HASH",
            f"{name} must be a 64-character SHA-256 hex digest",
            details={"field": name},
        )
    return value.lower()


def _require_stable_hash(
    value: Any,
    name: str,
    *,
    allow_empty: bool = False,
) -> str:
    if allow_empty and (value is None or value == ""):
        return ""
    if not isinstance(value, str) or _STABLE_HASH_RE.fullmatch(value) is None:
        raise ExtractionJobError(
            "EXTRACTION_JOB_INVALID_HASH",
            f"{name} must be a SHA-256 or typed stable hash",
            details={"field": name},
        )
    prefix, separator, digest = value.rpartition("_")
    if separator and len(digest) == 64 and _SHA256_RE.fullmatch(digest):
        return f"{prefix}_{digest.lower()}"
    return value.lower()


def _require_int(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    allow_none: bool = False,
) -> int | None:
    if allow_none and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExtractionJobError(
            "EXTRACTION_JOB_INVALID_INTEGER",
            f"{name} must be an integer",
            details={"field": name},
        )
    if value < minimum:
        raise ExtractionJobError(
            "EXTRACTION_JOB_INVALID_INTEGER",
            f"{name} must be >= {minimum}",
            details={"field": name, "minimum": minimum},
        )
    return value


def _require_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExtractionJobError(
            "EXTRACTION_JOB_INVALID_ARGUMENT",
            "min_confidence must be a finite number in [0, 1]",
            details={"field": "min_confidence"},
        )
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ExtractionJobError(
            "EXTRACTION_JOB_INVALID_ARGUMENT",
            "min_confidence must be a finite number in [0, 1]",
            details={"field": "min_confidence"},
        )
    return result


def _require_positive_seconds(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExtractionJobError(
            "EXTRACTION_JOB_INVALID_ARGUMENT",
            f"{name} must be a finite positive number",
            details={"field": name},
        )
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ExtractionJobError(
            "EXTRACTION_JOB_INVALID_ARGUMENT",
            f"{name} must be a finite positive number",
            details={"field": name},
        )
    return result


def _canonical_json(value: Any, name: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExtractionJobError(
            "EXTRACTION_JOB_INVALID_JSON",
            f"{name} must be canonical JSON data",
            details={"field": name},
        ) from exc


def _json_object(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ExtractionJobError(
            "EXTRACTION_JOB_INVALID_JSON",
            f"{name} must be a JSON object",
            details={"field": name},
        )
    # Round-trip to detach caller-owned mutable containers and reject objects
    # that the canonical serializer cannot represent.
    return json.loads(_canonical_json(dict(value), name))


def _normalize_generation_params(value: Any) -> dict[str, Any]:
    """Bind extraction protocol metadata into the immutable job identity."""

    generation = _json_object(value, "generation_params")
    authoritative = generation.get(
        "authoritative_protocol",
        "json_object",
    )
    if authoritative != "json_object":
        raise ExtractionJobError(
            "EXTRACTION_PROTOCOL_INVALID",
            "authoritative extraction protocol must remain json_object",
            details={"field": "generation_params.authoritative_protocol"},
        )
    response_format_hash = generation.get(
        "authoritative_response_format_hash",
        "",
    )
    if response_format_hash:
        response_format_hash = _require_hash(
            response_format_hash,
            "generation_params.authoritative_response_format_hash",
        )
    tool_shadow = _json_object(
        generation.get("tool_shadow"),
        "generation_params.tool_shadow",
    )
    enabled = tool_shadow.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ExtractionJobError(
            "EXTRACTION_PROTOCOL_INVALID",
            "generation_params.tool_shadow.enabled must be a boolean",
            details={"field": "generation_params.tool_shadow.enabled"},
        )
    protocol = tool_shadow.get(
        "protocol",
        "tool_function_arguments",
    )
    if protocol != "tool_function_arguments":
        raise ExtractionJobError(
            "EXTRACTION_PROTOCOL_INVALID",
            "tool shadow protocol must be tool_function_arguments",
            details={"field": "generation_params.tool_shadow.protocol"},
        )
    tool_name = tool_shadow.get("tool_name", "")
    schema_hash = tool_shadow.get("schema_hash", "")
    if enabled:
        if (
            not isinstance(tool_name, str)
            or _TOOL_NAME_RE.fullmatch(tool_name.strip()) is None
        ):
            raise ExtractionJobError(
                "EXTRACTION_PROTOCOL_INVALID",
                "enabled tool shadow requires a portable tool_name",
                details={"field": "generation_params.tool_shadow.tool_name"},
            )
        tool_name = tool_name.strip()
        schema_hash = _require_hash(
            schema_hash,
            "generation_params.tool_shadow.schema_hash",
        )
    else:
        if tool_name:
            if (
                not isinstance(tool_name, str)
                or _TOOL_NAME_RE.fullmatch(tool_name.strip()) is None
            ):
                raise ExtractionJobError(
                    "EXTRACTION_PROTOCOL_INVALID",
                    "tool shadow tool_name is invalid",
                    details={
                        "field": "generation_params.tool_shadow.tool_name"
                    },
                )
            tool_name = tool_name.strip()
        if schema_hash:
            schema_hash = _require_hash(
                schema_hash,
                "generation_params.tool_shadow.schema_hash",
            )
    acceptance_eligible = tool_shadow.get(
        "acceptance_eligible",
        False,
    )
    if acceptance_eligible is not False:
        raise ExtractionJobError(
            "EXTRACTION_SHADOW_ACCEPTANCE_FORBIDDEN",
            "tool shadow output is diagnostic-only",
            details={
                "field": "generation_params.tool_shadow.acceptance_eligible"
            },
        )
    generation["authoritative_protocol"] = "json_object"
    if response_format_hash:
        generation[
            "authoritative_response_format_hash"
        ] = response_format_hash
    generation["tool_shadow"] = {
        "enabled": enabled,
        "protocol": "tool_function_arguments",
        "tool_name": tool_name,
        "schema_hash": schema_hash,
        "acceptance_eligible": False,
    }
    return generation


def _normalize_artifact_context(
    value: Any,
    *,
    branch_id: str,
) -> dict[str, Any]:
    artifact = _json_object(value, "artifact_context")
    artifact_id = _require_string(
        artifact.get("artifact_id"),
        "artifact_context.artifact_id",
    )
    artifact_stage = str(
        artifact.get("artifact_stage") or "brainstorm"
    ).strip()
    if not artifact_stage:
        raise ExtractionJobError(
            "EXTRACTION_ARTIFACT_IDENTITY_INVALID",
            "artifact_context.artifact_stage must be a non-empty string",
            details={"field": "artifact_context.artifact_stage"},
        )
    artifact_branch = str(
        artifact.get("branch_id") or branch_id
    ).strip()
    if artifact_branch != branch_id:
        raise ExtractionJobConflict(
            "EXTRACTION_ARTIFACT_IDENTITY_MISMATCH",
            "artifact_context.branch_id differs from the extraction job",
            details={
                "artifact_branch_id": artifact_branch,
                "job_branch_id": branch_id,
            },
        )
    chapter_no = _require_int(
        artifact.get("chapter_no"),
        "artifact_context.chapter_no",
        minimum=1,
        allow_none=True,
    )
    scene_index = _require_int(
        artifact.get("scene_index"),
        "artifact_context.scene_index",
        allow_none=True,
    )
    artifact_revision = _require_int(
        artifact.get("artifact_revision", 1),
        "artifact_context.artifact_revision",
        minimum=1,
    )
    assert artifact_revision is not None
    artifact.update(
        {
            "artifact_id": artifact_id,
            "artifact_stage": artifact_stage,
            "branch_id": artifact_branch,
            "chapter_no": chapter_no,
            "scene_index": scene_index,
            "artifact_revision": artifact_revision,
        }
    )
    return json.loads(_canonical_json(artifact, "artifact_context"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_error(value: BaseException | str) -> str:
    text = str(value).strip() or type(value).__name__
    environment_secrets = {
        str(secret).strip()
        for name, secret in os.environ.items()
        if _SECRET_ENV_NAME_RE.search(str(name))
        and len(str(secret).strip()) >= 8
    }
    for secret in sorted(environment_secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    redacted = _SECRET_RE.sub("[REDACTED]", text)
    return redacted[:2000]


def _parse_timestamp(value: str | datetime, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if _RFC3339_RE.fullmatch(text) is None:
            raise ExtractionJobError(
                "EXTRACTION_JOB_INVALID_TIMESTAMP",
                f"{name} must use RFC3339 date-time syntax",
                details={"field": name},
            )
        try:
            parsed = datetime.fromisoformat(
                text[:-1] + "+00:00" if text.endswith("Z") else text
            )
        except ValueError as exc:
            raise ExtractionJobError(
                "EXTRACTION_JOB_INVALID_TIMESTAMP",
                f"{name} must be an ISO-8601 timestamp",
                details={"field": name},
            ) from exc
    else:
        raise ExtractionJobError(
            "EXTRACTION_JOB_INVALID_TIMESTAMP",
            f"{name} must be an ISO-8601 timestamp",
            details={"field": name},
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExtractionJobError(
            "EXTRACTION_JOB_INVALID_TIMESTAMP",
            f"{name} must include a timezone",
            details={"field": name},
        )
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


class ExtractionJobQueue:
    """Transactional SQLite queue for asynchronous proposal extraction."""

    def __init__(
        self,
        project_root_or_store: str | Path | ContinuityStore,
        *,
        db_path: str | Path | None = None,
        clock: Callable[[], datetime | str] | None = None,
    ) -> None:
        if isinstance(project_root_or_store, ContinuityStore):
            if db_path is not None:
                raise ExtractionJobError(
                    "EXTRACTION_JOB_INVALID_ARGUMENT",
                    "db_path cannot be supplied with an existing store",
                    details={"field": "db_path"},
                )
            self.store = project_root_or_store
        else:
            self.store = ContinuityStore(
                project_root_or_store,
                db_path=db_path,
            )
        self._clock = clock

    def _now(self, supplied: str | datetime | None = None) -> datetime:
        if supplied is not None:
            return _parse_timestamp(supplied, "now")
        if self._clock is None:
            return datetime.now(timezone.utc)
        return _parse_timestamp(self._clock(), "clock")

    @staticmethod
    def _hash_binding(payload: Mapping[str, Any]) -> dict[str, str]:
        endpoint_hash = _sha256_text(str(payload["extract_base_url"]))
        model_hash = _sha256_text(
            f"{payload['extract_provider']}\0{payload['extract_model']}"
        )
        generation_hash = _sha256_text(
            str(payload["generation_params_json"])
        )
        immutable = {
            key: payload[key]
            for key in (
                "receipt_id",
                "request_id",
                "assistant_sha256",
                "prompt_hash",
                "retrieved_context_digest",
                "prepared_canon_revision",
                "active_projection_hash",
                "intent_contract_hash",
                "event_seed_manifest_hash",
                "event_experience_control_revision",
                "event_seed_references_json",
                "experience_contract_hashes_json",
                "artifact_context_json",
                "branch_id",
                "sequence_no",
                "extract_provider",
                "extract_base_url",
                "extract_model",
                "extract_schema_hash",
                "extract_prompt_template_hash",
                "min_confidence",
                "generation_params_json",
            )
        }
        binding_hash = _sha256_text(
            _canonical_json(immutable, "job_binding")
        )
        return {
            "extract_endpoint_hash": endpoint_hash,
            "extract_model_hash": model_hash,
            "generation_params_hash": generation_hash,
            "job_binding_hash": binding_hash,
        }

    @classmethod
    def _decode_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(row)
        try:
            experience = json.loads(
                str(raw["experience_contract_hashes_json"])
            )
            seed_references = json.loads(
                str(raw["event_seed_references_json"])
            )
            artifact = json.loads(str(raw["artifact_context_json"]))
            generation = json.loads(str(raw["generation_params_json"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ExtractionJobError(
                "EXTRACTION_JOB_CORRUPT",
                "persisted extraction job contains invalid JSON",
                details={"job_id": raw.get("job_id", "")},
            ) from exc
        if not isinstance(experience, list) or not isinstance(
            seed_references, list
        ):
            raise ExtractionJobError(
                "EXTRACTION_JOB_CORRUPT",
                "event-experience bindings are not arrays",
                details={"job_id": raw.get("job_id", "")},
            )
        if not isinstance(artifact, dict) or not isinstance(generation, dict):
            raise ExtractionJobError(
                "EXTRACTION_JOB_CORRUPT",
                "persisted extraction job JSON objects have invalid shapes",
                details={"job_id": raw.get("job_id", "")},
            )
        raw["experience_contract_hashes"] = experience
        raw["event_seed_references"] = seed_references
        raw["artifact_context"] = artifact
        raw["generation_params"] = generation
        raw["status"] = str(raw["job_status"])
        raw["lease_epoch"] = int(raw["attempt_count"])
        if str(raw.get("error") or ""):
            raw["error"] = _safe_error(str(raw["error"]))
        hashes = cls._hash_binding(raw)
        persisted_binding_hash = str(raw.get("job_binding_hash") or "")
        if persisted_binding_hash != hashes["job_binding_hash"]:
            raise ExtractionJobError(
                "EXTRACTION_JOB_BINDING_CORRUPT",
                "persisted immutable job binding hash does not match",
                details={"job_id": raw.get("job_id", "")},
            )
        raw.update(hashes)
        raw["hash_binding"] = {
            "assistant_sha256": str(raw["assistant_sha256"]),
            "prompt_hash": str(raw["prompt_hash"]),
            "retrieved_context_digest": str(
                raw["retrieved_context_digest"]
            ),
            "active_projection_hash": str(
                raw["active_projection_hash"]
            ),
            "intent_contract_hash": str(raw["intent_contract_hash"]),
            "event_seed_manifest_hash": str(
                raw["event_seed_manifest_hash"]
            ),
            "event_experience_control_revision": int(
                raw["event_experience_control_revision"]
            ),
            "extract_schema_hash": str(raw["extract_schema_hash"]),
            "extract_prompt_template_hash": str(
                raw["extract_prompt_template_hash"]
            ),
            **hashes,
        }
        for key in (
            "experience_contract_hashes_json",
            "event_seed_references_json",
            "artifact_context_json",
            "generation_params_json",
        ):
            raw.pop(key, None)
        return raw

    @staticmethod
    def _row_payload(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(row)
        return {
            key: raw[key]
            for key in (
                "receipt_id",
                "request_id",
                "assistant_sha256",
                "prompt_hash",
                "retrieved_context_digest",
                "prepared_canon_revision",
                "active_projection_hash",
                "intent_contract_hash",
                "event_seed_manifest_hash",
                "event_experience_control_revision",
                "event_seed_references_json",
                "experience_contract_hashes_json",
                "artifact_context_json",
                "branch_id",
                "sequence_no",
                "extract_provider",
                "extract_base_url",
                "extract_model",
                "extract_schema_hash",
                "extract_prompt_template_hash",
                "min_confidence",
                "generation_params_json",
            )
        }

    @staticmethod
    def _is_bound_async_shadow_receipt(
        connection: sqlite3.Connection,
        receipt: sqlite3.Row | Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> bool:
        def decode_object(value: Any) -> dict[str, Any] | None:
            try:
                decoded = json.loads(str(value))
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            return decoded if isinstance(decoded, dict) else None

        def decode_list(value: Any) -> list[Any] | None:
            try:
                decoded = json.loads(str(value))
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            return decoded if isinstance(decoded, list) else None

        try:
            artifact_context = json.loads(
                str(payload["artifact_context_json"])
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(artifact_context, dict):
            return False
        shadow = artifact_context.get("_plot_rag_v15")
        if not isinstance(shadow, dict):
            return False
        if str(shadow.get("extraction_execution_mode") or "") != (
            "async_shadow"
        ):
            return False
        authoritative_id = str(
            shadow.get("authoritative_proposal_id") or ""
        ).strip()
        if _STABLE_HASH_RE.fullmatch(authoritative_id) is None:
            return False
        try:
            shadow_receipt = connection.execute(
                """
                SELECT result_json, prepared_canon_revision, v1_context_json,
                       active_projection_hash, retrieved_context_digest,
                       lifecycle_identity_json
                FROM turns
                WHERE receipt_id=?
                """,
                (str(receipt["receipt_id"]),),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        if shadow_receipt is None:
            return False
        result = decode_object(shadow_receipt["result_json"])
        prepared_artifact = decode_object(
            shadow_receipt["v1_context_json"]
        )
        prepared_lifecycle = decode_object(
            shadow_receipt["lifecycle_identity_json"]
        )
        event_seed_references = decode_list(
            payload["event_seed_references_json"]
        )
        experience_contract_hashes = decode_list(
            payload["experience_contract_hashes_json"]
        )
        if (
            result is None
            or prepared_artifact is None
            or prepared_lifecycle is None
            or event_seed_references is None
            or experience_contract_hashes is None
            or str(result.get("proposal_id") or "") != authoritative_id
            or str(receipt["assistant_hash"] or "")
            != str(payload["assistant_sha256"])
            or str(shadow_receipt["retrieved_context_digest"] or "")
            != str(payload["retrieved_context_digest"])
            or int(shadow_receipt["prepared_canon_revision"])
            != int(payload["prepared_canon_revision"])
            or str(shadow_receipt["active_projection_hash"] or "")
            != str(payload["active_projection_hash"])
        ):
            return False
        expected_lifecycle = {
            "intent_contract_hash": str(
                payload["intent_contract_hash"] or ""
            ),
            "event_seed_manifest_hash": str(
                payload["event_seed_manifest_hash"] or ""
            ),
            "event_experience_control_revision": int(
                payload["event_experience_control_revision"]
            ),
            "event_seed_references": event_seed_references,
            "experience_contract_hashes": experience_contract_hashes,
        }
        if any(
            prepared_lifecycle.get(key) != expected
            for key, expected in expected_lifecycle.items()
        ):
            return False
        shadow_lifecycle = {
            "intent_contract_hash": str(
                shadow.get("intent_contract_hash") or ""
            ),
            "event_seed_manifest_hash": str(
                shadow.get("event_seed_manifest_hash") or ""
            ),
            "event_experience_control_revision": shadow.get(
                "event_experience_control_revision"
            ),
            "event_seed_references": shadow.get("event_seed_references"),
        }
        if any(
            shadow_lifecycle.get(key) != expected_lifecycle[key]
            for key in shadow_lifecycle
        ):
            return False
        authoritative = connection.execute(
            """
            SELECT proposal_id, artifact_id, artifact_stage, branch_id,
                   chapter_no, scene_index, artifact_revision,
                   prepared_canon_revision, canon_status, validation_status,
                   accepted_commit_id, payload_json
            FROM proposals
            WHERE proposal_id=?
            """,
            (authoritative_id,),
        ).fetchone()
        if (
            authoritative is None
            or str(authoritative["canon_status"]) != "proposed"
            or str(authoritative["validation_status"]) != "valid"
            or authoritative["accepted_commit_id"] is not None
        ):
            return False
        try:
            authoritative_payload = json.loads(
                str(authoritative["payload_json"])
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if (
            not isinstance(authoritative_payload, dict)
            or authoritative_payload.get("extraction_shadow")
        ):
            return False
        for proposal_field, job_field in (
            ("receipt_id", "receipt_id"),
            ("request_id", "request_id"),
            ("assistant_sha256", "assistant_sha256"),
            ("prompt_hash", "prompt_hash"),
            ("retrieved_context_digest", "retrieved_context_digest"),
            ("prepared_canon_revision", "prepared_canon_revision"),
            ("active_projection_hash", "active_projection_hash"),
        ):
            if authoritative_payload.get(proposal_field) != payload.get(
                job_field
            ):
                return False
        authoritative_lifecycle = authoritative_payload.get(
            "lifecycle_identity"
        )
        if not isinstance(authoritative_lifecycle, Mapping) or any(
            authoritative_lifecycle.get(key) != expected
            for key, expected in expected_lifecycle.items()
        ):
            return False
        identity_fields = (
            "artifact_id",
            "artifact_stage",
            "branch_id",
            "chapter_no",
            "scene_index",
        )
        prepared_identity = {
            field: prepared_artifact.get(field) for field in identity_fields
        }
        job_identity = {
            field: artifact_context.get(field) for field in identity_fields
        }
        authoritative_identity = {
            "artifact_id": str(authoritative["artifact_id"]),
            "artifact_stage": str(authoritative["artifact_stage"]),
            "branch_id": str(authoritative["branch_id"]),
            "chapter_no": authoritative["chapter_no"],
            "scene_index": authoritative["scene_index"],
        }
        authoritative_artifact = authoritative_payload.get(
            "artifact_context"
        )
        if (
            job_identity != prepared_identity
            or authoritative_identity != prepared_identity
            or not isinstance(authoritative_artifact, Mapping)
            or any(
                authoritative_artifact.get(field) != expected
                for field, expected in prepared_identity.items()
            )
            or int(authoritative["prepared_canon_revision"])
            != int(payload["prepared_canon_revision"])
        ):
            return False
        authoritative_revision = int(authoritative["artifact_revision"])
        if (
            "artifact_revision" in prepared_artifact
            and int(prepared_artifact["artifact_revision"])
            != authoritative_revision
        ):
            return False
        if (
            "artifact_revision" in authoritative_artifact
            and int(authoritative_artifact["artifact_revision"])
            != authoritative_revision
        ):
            return False
        return (
            int(artifact_context.get("artifact_revision") or 0)
            == authoritative_revision + 1
            and int(shadow.get("authoritative_artifact_revision") or 0)
            == authoritative_revision
        )

    @staticmethod
    def _assert_receipt_binding(
        connection: sqlite3.Connection,
        payload: Mapping[str, Any],
    ) -> sqlite3.Row:
        receipt_id = str(payload["receipt_id"])
        receipt = connection.execute(
            """
            SELECT receipt_id, request_id, prompt_hash, assistant_hash, status
            FROM turns
            WHERE receipt_id=?
            """,
            (receipt_id,),
        ).fetchone()
        if receipt is None:
            raise ExtractionJobConflict(
                "EXTRACTION_RECEIPT_NOT_FOUND",
                "extraction job requires an existing Prepare receipt",
                details={"receipt_id": receipt_id},
            )
        mismatches: dict[str, dict[str, Any]] = {}
        for receipt_field, job_field in (
            ("request_id", "request_id"),
            ("prompt_hash", "prompt_hash"),
        ):
            expected = str(payload[job_field])
            actual = str(receipt[receipt_field])
            if actual != expected:
                mismatches[job_field] = {
                    "expected": actual,
                    "actual": expected,
                }
        stored_assistant_hash = str(receipt["assistant_hash"] or "")
        if (
            stored_assistant_hash
            and stored_assistant_hash != str(payload["assistant_sha256"])
        ):
            mismatches["assistant_sha256"] = {
                "expected": stored_assistant_hash,
                "actual": str(payload["assistant_sha256"]),
            }
        if mismatches:
            raise ExtractionJobConflict(
                "EXTRACTION_RECEIPT_BINDING_MISMATCH",
                "extraction inputs differ from their Prepare receipt",
                details={
                    "receipt_id": receipt_id,
                    "mismatches": mismatches,
                },
            )
        receipt_status = str(receipt["status"])
        shadow_receipt = (
            receipt_status == "proposed"
            and ExtractionJobQueue._is_bound_async_shadow_receipt(
                connection,
                receipt,
                payload,
            )
        )
        if (
            receipt_status not in {"pending", "failed", "committed"}
            and not shadow_receipt
        ):
            raise ExtractionJobConflict(
                "EXTRACTION_RECEIPT_STATUS_CONFLICT",
                "Prepare receipt is not in an extractable state",
                details={
                    "receipt_id": receipt_id,
                    "status": receipt_status,
                },
            )
        return receipt

    def enqueue(
        self,
        *,
        receipt_id: str,
        request_id: str,
        prompt_hash: str,
        retrieved_context_digest: str,
        prepared_canon_revision: int,
        active_projection_hash: str,
        extract_provider: str,
        extract_base_url: str,
        extract_model: str,
        extract_schema_hash: str,
        extract_prompt_template_hash: str,
        min_confidence: float,
        assistant_sha256: str | None = None,
        assistant_text: str | None = None,
        intent_contract_hash: str = "",
        event_seed_manifest_hash: str = "",
        event_experience_control_revision: int = 0,
        event_seed_references: Sequence[Mapping[str, Any]] = (),
        experience_contract_hashes: Sequence[str] = (),
        artifact_context: Mapping[str, Any] | None = None,
        branch_id: str = "main",
        sequence_no: int | None = None,
        generation_params: Mapping[str, Any] | None = None,
        job_id: str | None = None,
        extract_endpoint_hash: str | None = None,
        extract_model_hash: str | None = None,
        generation_params_hash: str | None = None,
        job_binding_hash: str | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Persist a queued job and return immediately.

        ``receipt_id + assistant_sha256`` is the idempotency key.  Reusing the
        key with a different immutable binding raises a conflict instead of
        silently changing the extraction inputs.
        """

        started = time.perf_counter()
        receipt_id = _require_string(receipt_id, "receipt_id")
        request_id = _require_string(request_id, "request_id")
        if assistant_text is not None and not isinstance(assistant_text, str):
            raise ExtractionJobError(
                "EXTRACTION_JOB_INVALID_ARGUMENT",
                "assistant_text must be a string",
                details={"field": "assistant_text"},
            )
        assistant_payload_bytes = (
            assistant_text.encode("utf-8")
            if assistant_text is not None
            else None
        )
        if (
            assistant_payload_bytes is not None
            and len(assistant_payload_bytes) > MAX_ASSISTANT_PAYLOAD_BYTES
        ):
            raise ExtractionJobError(
                "EXTRACTION_JOB_PAYLOAD_TOO_LARGE",
                "assistant_text exceeds the durable payload limit",
                details={
                    "payload_bytes": len(assistant_payload_bytes),
                    "maximum": MAX_ASSISTANT_PAYLOAD_BYTES,
                },
            )
        computed_assistant_hash = (
            _sha256_text(assistant_text) if assistant_text is not None else None
        )
        if assistant_sha256 is None:
            if computed_assistant_hash is None:
                raise ExtractionJobError(
                    "EXTRACTION_JOB_INVALID_HASH",
                    "assistant_sha256 or assistant_text is required",
                    details={"field": "assistant_sha256"},
                )
            assistant_sha256 = computed_assistant_hash
        assistant_sha256 = _require_hash(
            assistant_sha256,
            "assistant_sha256",
        )
        if (
            computed_assistant_hash is not None
            and assistant_sha256 != computed_assistant_hash
        ):
            raise ExtractionJobConflict(
                "EXTRACTION_ASSISTANT_HASH_MISMATCH",
                "assistant_text does not match assistant_sha256",
            )

        prompt_hash = _require_hash(prompt_hash, "prompt_hash")
        retrieved_context_digest = _require_hash(
            retrieved_context_digest,
            "retrieved_context_digest",
        )
        active_projection_hash = _require_stable_hash(
            active_projection_hash,
            "active_projection_hash",
        )
        intent_contract_hash = _require_stable_hash(
            intent_contract_hash,
            "intent_contract_hash",
            allow_empty=True,
        )
        event_seed_manifest_hash = _require_stable_hash(
            event_seed_manifest_hash,
            "event_seed_manifest_hash",
            allow_empty=True,
        )
        control_revision = _require_int(
            event_experience_control_revision,
            "event_experience_control_revision",
        )
        assert control_revision is not None
        extract_schema_hash = _require_hash(
            extract_schema_hash,
            "extract_schema_hash",
        )
        extract_prompt_template_hash = _require_hash(
            extract_prompt_template_hash,
            "extract_prompt_template_hash",
        )
        prepared_revision = _require_int(
            prepared_canon_revision,
            "prepared_canon_revision",
        )
        if sequence_no is None:
            raise ExtractionJobError(
                "EXTRACTION_SEQUENCE_REQUIRED",
                "asynchronous story extraction requires sequence_no",
                details={"field": "sequence_no"},
            )
        sequence = _require_int(sequence_no, "sequence_no")
        assert sequence is not None
        confidence = _require_confidence(min_confidence)
        provider = _require_string(extract_provider, "extract_provider")
        base_url = _require_string(extract_base_url, "extract_base_url")
        model = _require_string(extract_model, "extract_model")
        branch = _require_string(branch_id, "branch_id")

        if isinstance(experience_contract_hashes, (str, bytes)):
            raise ExtractionJobError(
                "EXTRACTION_JOB_INVALID_ARGUMENT",
                "experience_contract_hashes must be a sequence of hashes",
                details={"field": "experience_contract_hashes"},
            )
        experience_hashes = [
            _require_stable_hash(
                value,
                f"experience_contract_hashes[{index}]",
            )
            for index, value in enumerate(experience_contract_hashes)
        ]
        if isinstance(event_seed_references, (str, bytes)):
            raise ExtractionJobError(
                "EXTRACTION_JOB_INVALID_ARGUMENT",
                "event_seed_references must be a sequence of objects",
                details={"field": "event_seed_references"},
            )
        seed_references: list[dict[str, Any]] = []
        for index, value in enumerate(event_seed_references):
            reference = _json_object(
                value,
                f"event_seed_references[{index}]",
            )
            seed_id = _require_string(
                reference.get("event_seed_id"),
                f"event_seed_references[{index}].event_seed_id",
            )
            seed_revision = _require_int(
                reference.get("event_seed_revision"),
                f"event_seed_references[{index}].event_seed_revision",
                minimum=1,
            )
            seed_references.append(
                {
                    **reference,
                    "event_seed_id": seed_id,
                    "event_seed_revision": seed_revision,
                }
            )
        if bool(event_seed_manifest_hash) != bool(seed_references):
            raise ExtractionJobError(
                "EXTRACTION_EVENT_EXPERIENCE_BINDING_INCOMPLETE",
                "event_seed_manifest_hash and event_seed_references must "
                "either both be present or both be absent",
            )
        if not seed_references and (
            control_revision != 0 or experience_hashes
        ):
            raise ExtractionJobError(
                "EXTRACTION_EVENT_EXPERIENCE_BINDING_INCOMPLETE",
                "control revision and contract hashes require seed "
                "references",
            )
        artifact = _normalize_artifact_context(
            artifact_context,
            branch_id=branch,
        )
        generation = _normalize_generation_params(generation_params)
        timestamp = _format_timestamp(self._now(now))
        requested_job_id = (
            _require_string(job_id, "job_id")
            if job_id is not None
            else f"extract-{uuid.uuid4().hex}"
        )

        payload: dict[str, Any] = {
            "receipt_id": receipt_id,
            "request_id": request_id,
            "assistant_sha256": assistant_sha256,
            "prompt_hash": prompt_hash,
            "retrieved_context_digest": retrieved_context_digest,
            "prepared_canon_revision": prepared_revision,
            "active_projection_hash": active_projection_hash,
            "intent_contract_hash": intent_contract_hash,
            "event_seed_manifest_hash": event_seed_manifest_hash,
            "event_experience_control_revision": control_revision,
            "event_seed_references_json": _canonical_json(
                seed_references,
                "event_seed_references",
            ),
            "experience_contract_hashes_json": _canonical_json(
                experience_hashes,
                "experience_contract_hashes",
            ),
            "artifact_context_json": _canonical_json(
                artifact,
                "artifact_context",
            ),
            "branch_id": branch,
            "sequence_no": sequence,
            "extract_provider": provider,
            "extract_base_url": base_url,
            "extract_model": model,
            "extract_schema_hash": extract_schema_hash,
            "extract_prompt_template_hash": extract_prompt_template_hash,
            "min_confidence": confidence,
            "generation_params_json": _canonical_json(
                generation,
                "generation_params",
            ),
        }
        derived_hashes = self._hash_binding(payload)
        payload["job_binding_hash"] = derived_hashes["job_binding_hash"]
        expected_hashes = {
            "extract_endpoint_hash": extract_endpoint_hash,
            "extract_model_hash": extract_model_hash,
            "generation_params_hash": generation_params_hash,
            "job_binding_hash": job_binding_hash,
        }
        for name, expected in expected_hashes.items():
            if expected is None:
                continue
            expected_digest = _require_hash(expected, name)
            if expected_digest != derived_hashes[name]:
                raise ExtractionJobConflict(
                    "EXTRACTION_BINDING_HASH_MISMATCH",
                    f"{name} does not match the immutable job binding",
                    details={
                        "field": name,
                        "expected": expected_digest,
                        "actual": derived_hashes[name],
                    },
                )

        reused = False
        try:
            with self.store.transaction() as connection:
                self._assert_receipt_binding(connection, payload)
                existing = connection.execute(
                    """
                    SELECT *
                    FROM extraction_jobs
                    WHERE receipt_id=? AND assistant_sha256=?
                    """,
                    (receipt_id, assistant_sha256),
                ).fetchone()
                if existing is not None:
                    self._assert_persisted_binding(existing)
                    existing_hash = self._hash_binding(
                        self._row_payload(existing)
                    )["job_binding_hash"]
                    if existing_hash != derived_hashes["job_binding_hash"]:
                        raise ExtractionJobConflict(
                            "EXTRACTION_JOB_IDEMPOTENCY_CONFLICT",
                            "receipt_id + assistant_sha256 is already bound "
                            "to different extraction inputs",
                            details={
                                "job_id": str(existing["job_id"]),
                                "existing_binding_hash": existing_hash,
                                "requested_binding_hash": derived_hashes[
                                    "job_binding_hash"
                                ],
                            },
                        )
                    persisted_payload = connection.execute(
                        """
                        SELECT assistant_text, assistant_sha256, payload_bytes
                        FROM extraction_job_payloads
                        WHERE job_id=?
                        """,
                        (str(existing["job_id"]),),
                    ).fetchone()
                    if persisted_payload is None:
                        recoverable = str(existing["job_status"]) in {
                            "queued",
                            "running",
                            "failed",
                        }
                        if recoverable and assistant_text is not None:
                            connection.execute(
                                """
                                INSERT INTO extraction_job_payloads(
                                    job_id, assistant_text, assistant_sha256,
                                    payload_bytes, created_at, updated_at
                                ) VALUES(?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    str(existing["job_id"]),
                                    assistant_text,
                                    assistant_sha256,
                                    len(assistant_payload_bytes or b""),
                                    timestamp,
                                    timestamp,
                                ),
                            )
                        elif recoverable:
                            raise ExtractionJobError(
                                "EXTRACTION_JOB_PAYLOAD_MISSING",
                                "recoverable extraction job has no durable "
                                "assistant payload",
                                details={"job_id": str(existing["job_id"])},
                            )
                    else:
                        persisted_text = str(
                            persisted_payload["assistant_text"]
                        )
                        persisted_hash = _sha256_text(persisted_text)
                        persisted_bytes = len(persisted_text.encode("utf-8"))
                        if (
                            persisted_hash != assistant_sha256
                            or str(persisted_payload["assistant_sha256"])
                            != assistant_sha256
                            or int(persisted_payload["payload_bytes"])
                            != persisted_bytes
                        ):
                            raise ExtractionJobError(
                                "EXTRACTION_JOB_PAYLOAD_CORRUPT",
                                "durable assistant payload does not match "
                                "the job hash",
                                details={"job_id": str(existing["job_id"])},
                            )
                        if (
                            assistant_text is not None
                            and assistant_text != persisted_text
                        ):
                            raise ExtractionJobConflict(
                                "EXTRACTION_JOB_PAYLOAD_CONFLICT",
                                "assistant payload differs for the same "
                                "idempotency key",
                                details={"job_id": str(existing["job_id"])},
                            )
                    row = existing
                    reused = True
                else:
                    if assistant_text is None:
                        raise ExtractionJobError(
                            "EXTRACTION_JOB_PAYLOAD_REQUIRED",
                            "a new asynchronous extraction job requires "
                            "assistant_text for restart recovery",
                        )
                    connection.execute(
                        """
                        INSERT INTO extraction_jobs(
                            job_id, receipt_id, request_id,
                            assistant_sha256, prompt_hash,
                            retrieved_context_digest,
                            prepared_canon_revision,
                            active_projection_hash, intent_contract_hash,
                            event_seed_manifest_hash,
                            event_experience_control_revision,
                            event_seed_references_json,
                            experience_contract_hashes_json,
                            artifact_context_json, branch_id, sequence_no,
                            extract_provider, extract_base_url,
                            extract_model, extract_schema_hash,
                            extract_prompt_template_hash, min_confidence,
                            generation_params_json, job_binding_hash,
                            job_status, attempt_count, remote_status,
                            result_kind, error,
                            lease_owner, created_at, updated_at
                        ) VALUES(
                            :job_id, :receipt_id, :request_id,
                            :assistant_sha256, :prompt_hash,
                            :retrieved_context_digest,
                            :prepared_canon_revision,
                            :active_projection_hash, :intent_contract_hash,
                            :event_seed_manifest_hash,
                            :event_experience_control_revision,
                            :event_seed_references_json,
                            :experience_contract_hashes_json,
                            :artifact_context_json, :branch_id, :sequence_no,
                            :extract_provider, :extract_base_url,
                            :extract_model, :extract_schema_hash,
                            :extract_prompt_template_hash, :min_confidence,
                            :generation_params_json, :job_binding_hash,
                            'queued', 0, '', '', '', '',
                            :created_at, :updated_at
                        )
                        """,
                        {
                            **payload,
                            "job_id": requested_job_id,
                            "created_at": timestamp,
                            "updated_at": timestamp,
                        },
                    )
                    connection.execute(
                        """
                        INSERT INTO extraction_job_payloads(
                            job_id, assistant_text, assistant_sha256,
                            payload_bytes, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            requested_job_id,
                            assistant_text,
                            assistant_sha256,
                            len(assistant_payload_bytes or b""),
                            timestamp,
                            timestamp,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM extraction_jobs WHERE job_id=?",
                        (requested_job_id,),
                    ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ExtractionJobConflict(
                "EXTRACTION_JOB_KEY_CONFLICT",
                "job_id or idempotency key already exists",
                details={"job_id": requested_job_id},
            ) from exc

        if row is None:
            raise ExtractionJobError(
                "EXTRACTION_JOB_PERSIST_FAILED",
                "job disappeared during enqueue",
            )
        result = self._decode_row(row)
        result["reused"] = reused
        result["enqueue_ms"] = (time.perf_counter() - started) * 1000.0
        return result

    enqueue_job = enqueue

    def inspect(self, job_id: str) -> dict[str, Any]:
        job_id = _require_string(job_id, "job_id")
        with self.store.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM extraction_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise ExtractionJobNotFound(
                "EXTRACTION_JOB_NOT_FOUND",
                f"no extraction job named {job_id}",
                details={"job_id": job_id},
            )
        return self._decode_row(row)

    get_job = inspect
    inspect_job = inspect

    @staticmethod
    def _proposal_binding_for_job(
        job: Mapping[str, Any],
    ) -> dict[str, Any]:
        artifact_context = _json_object(
            job["artifact_context"],
            "artifact_context",
        )
        return {
            "extraction_job_id": str(job["job_id"]),
            "job_binding_hash": str(job["job_binding_hash"]),
            "receipt_id": str(job["receipt_id"]),
            "request_id": str(job["request_id"]),
            "assistant_sha256": str(job["assistant_sha256"]),
            "prompt_hash": str(job["prompt_hash"]),
            "retrieved_context_digest": str(
                job["retrieved_context_digest"]
            ),
            "prepared_canon_revision": int(
                job["prepared_canon_revision"]
            ),
            "active_projection_hash": str(
                job["active_projection_hash"]
            ),
            "intent_contract_hash": str(job["intent_contract_hash"]),
            "event_seed_manifest_hash": str(
                job["event_seed_manifest_hash"]
            ),
            "event_experience_control_revision": int(
                job["event_experience_control_revision"]
            ),
            "event_seed_references": list(
                job["event_seed_references"]
            ),
            "experience_contract_hashes": list(
                job["experience_contract_hashes"]
            ),
            "artifact_context": artifact_context,
        }

    def proposal_binding(
        self,
        job_or_id: Mapping[str, Any] | str,
    ) -> dict[str, Any]:
        """Return the exact payload fragment a proposal must persist."""

        job = (
            self.inspect(job_or_id)
            if isinstance(job_or_id, str)
            else dict(job_or_id)
        )
        return self._proposal_binding_for_job(job)

    def list_jobs(
        self,
        *,
        status: str | Sequence[str] | None = None,
        branch_id: str | None = None,
        sequence_no: int | None = None,
        receipt_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        checked_limit = _require_int(limit, "limit", minimum=1)
        checked_offset = _require_int(offset, "offset")
        assert checked_limit is not None
        assert checked_offset is not None
        if checked_limit > _MAX_LIST_LIMIT:
            raise ExtractionJobError(
                "EXTRACTION_JOB_INVALID_INTEGER",
                f"limit must be <= {_MAX_LIST_LIMIT}",
                details={"field": "limit", "maximum": _MAX_LIST_LIMIT},
            )

        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            values = [status] if isinstance(status, str) else list(status)
            if not values:
                return []
            checked_statuses: list[str] = []
            for value in values:
                checked = _require_string(value, "status")
                if checked not in JOB_STATUSES:
                    raise ExtractionJobError(
                        "EXTRACTION_JOB_INVALID_STATUS",
                        f"unknown extraction job status: {checked}",
                        details={"status": checked},
                    )
                checked_statuses.append(checked)
            placeholders = ",".join("?" for _ in checked_statuses)
            clauses.append(f"job_status IN ({placeholders})")
            params.extend(checked_statuses)
        if branch_id is not None:
            clauses.append("branch_id=?")
            params.append(_require_string(branch_id, "branch_id"))
        if sequence_no is not None:
            clauses.append("sequence_no=?")
            params.append(_require_int(sequence_no, "sequence_no"))
        if receipt_id is not None:
            clauses.append("receipt_id=?")
            params.append(_require_string(receipt_id, "receipt_id"))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([checked_limit, checked_offset])
        with self.store.read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM extraction_jobs
                {where}
                ORDER BY created_at DESC, job_id DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def read_assistant_text(
        self,
        job_id: str,
        *,
        worker_id: str,
        expected_attempt_count: int,
        now: str | datetime | None = None,
    ) -> str:
        """Read the isolated payload only for the active fenced worker."""

        job_id = _require_string(job_id, "job_id")
        worker = _require_string(worker_id, "worker_id")
        epoch = self._require_epoch(expected_attempt_count)
        with self.store.read_connection() as connection:
            connection.execute("BEGIN")
            timestamp = _format_timestamp(self._now(now))
            row = connection.execute(
                """
                SELECT *
                FROM extraction_jobs
                WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise ExtractionJobNotFound(
                    "EXTRACTION_JOB_NOT_FOUND",
                    f"no extraction job named {job_id}",
                    details={"job_id": job_id},
                )
            if (
                str(row["job_status"]) != "running"
                or str(row["lease_owner"]) != worker
                or int(row["attempt_count"]) != epoch
                or row["lease_expires_at"] is None
                or str(row["lease_expires_at"]) <= timestamp
            ):
                raise self._lease_failure(job_id, worker, epoch, row)
            self._assert_persisted_binding(row)
            payload = connection.execute(
                """
                SELECT assistant_text, assistant_sha256, payload_bytes
                FROM extraction_job_payloads
                WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
        return self._validated_payload_text(row, payload)

    def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        branch_id: str | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        """Claim the next due queued job and increment its fencing epoch."""

        worker = _require_string(worker_id, "worker_id")
        seconds = _require_int(lease_seconds, "lease_seconds", minimum=1)
        assert seconds is not None
        branch = (
            _require_string(branch_id, "branch_id")
            if branch_id is not None
            else None
        )
        with self.store.transaction() as connection:
            now_dt = self._now(now)
            timestamp = _format_timestamp(now_dt)
            expires = _format_timestamp(
                now_dt + timedelta(seconds=seconds)
            )
            clauses = [
                "job_status='queued'",
                "(next_attempt_at IS NULL OR next_attempt_at<=?)",
            ]
            params: list[Any] = [timestamp]
            if branch is not None:
                clauses.append("branch_id=?")
                params.append(branch)
            candidate = connection.execute(
                f"""
                SELECT *
                FROM extraction_jobs
                WHERE {' AND '.join(clauses)}
                ORDER BY
                    CASE WHEN next_attempt_at IS NULL THEN 0 ELSE 1 END,
                    next_attempt_at,
                    created_at,
                    job_id
                LIMIT 1
                """,
                params,
            ).fetchone()
            if candidate is None:
                return None
            self._assert_persisted_binding(candidate)
            current_epoch = int(candidate["attempt_count"])
            cursor = connection.execute(
                """
                UPDATE extraction_jobs
                SET job_status='running',
                    attempt_count=attempt_count+1,
                    remote_status='claimed',
                    error='',
                    lease_owner=?,
                    lease_expires_at=?,
                    heartbeat_at=?,
                    next_attempt_at=NULL,
                    started_at=?,
                    completed_at=NULL,
                    updated_at=?
                WHERE job_id=? AND job_status='queued'
                  AND attempt_count=?
                  AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                """,
                (
                    worker,
                    expires,
                    timestamp,
                    timestamp,
                    timestamp,
                    str(candidate["job_id"]),
                    current_epoch,
                    timestamp,
                ),
            )
            if cursor.rowcount != 1:
                raise ExtractionJobConflict(
                    "EXTRACTION_JOB_CLAIM_CONFLICT",
                    "queued job changed before its lease was claimed",
                    details={"job_id": str(candidate["job_id"])},
                )
            row = connection.execute(
                "SELECT * FROM extraction_jobs WHERE job_id=?",
                (str(candidate["job_id"]),),
            ).fetchone()
        return self._decode_row(row)

    claim_next = claim

    @staticmethod
    def _require_epoch(value: Any) -> int:
        result = _require_int(
            value,
            "expected_attempt_count",
            minimum=1,
        )
        assert result is not None
        return result

    @staticmethod
    def _lease_failure(
        job_id: str,
        worker_id: str,
        expected_epoch: int,
        row: sqlite3.Row | None,
    ) -> ExtractionLeaseLost:
        details: dict[str, Any] = {
            "job_id": job_id,
            "worker_id": worker_id,
            "expected_attempt_count": expected_epoch,
        }
        if row is not None:
            details.update(
                {
                    "actual_status": str(row["job_status"]),
                    "actual_lease_owner": str(row["lease_owner"]),
                    "actual_attempt_count": int(row["attempt_count"]),
                    "lease_expires_at": row["lease_expires_at"],
                }
            )
        return ExtractionLeaseLost(
            "EXTRACTION_JOB_LEASE_LOST",
            "worker does not own the active, unexpired lease epoch",
            details=details,
        )

    @classmethod
    def _leased_row(
        cls,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        worker_id: str,
        expected_epoch: int,
        timestamp: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM extraction_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ExtractionJobNotFound(
                "EXTRACTION_JOB_NOT_FOUND",
                f"no extraction job named {job_id}",
                details={"job_id": job_id},
            )
        if (
            str(row["job_status"]) != "running"
            or str(row["lease_owner"]) != worker_id
            or int(row["attempt_count"]) != expected_epoch
            or row["lease_expires_at"] is None
            or str(row["lease_expires_at"]) <= timestamp
        ):
            raise cls._lease_failure(
                job_id,
                worker_id,
                expected_epoch,
                row,
            )
        cls._assert_persisted_binding(row)
        return row

    @classmethod
    def _assert_persisted_binding(
        cls,
        row: sqlite3.Row | Mapping[str, Any],
    ) -> dict[str, Any]:
        decoded = cls._decode_row(row)
        return decoded

    @staticmethod
    def _validated_payload_text(
        row: sqlite3.Row | Mapping[str, Any],
        payload: sqlite3.Row | Mapping[str, Any] | None,
    ) -> str:
        job_id = str(row["job_id"])
        if payload is None:
            raise ExtractionJobError(
                "EXTRACTION_JOB_PAYLOAD_MISSING",
                "claimed job has no durable assistant payload",
                details={"job_id": job_id},
            )
        assistant_text = str(payload["assistant_text"])
        actual_hash = _sha256_text(assistant_text)
        actual_bytes = len(assistant_text.encode("utf-8"))
        if (
            actual_hash != str(row["assistant_sha256"])
            or actual_hash != str(payload["assistant_sha256"])
            or actual_bytes != int(payload["payload_bytes"])
            or actual_bytes > MAX_ASSISTANT_PAYLOAD_BYTES
        ):
            raise ExtractionJobError(
                "EXTRACTION_JOB_PAYLOAD_CORRUPT",
                "durable assistant payload failed its hash/size check",
                details={"job_id": job_id},
            )
        return assistant_text

    def _assert_prepared_identity(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | Mapping[str, Any],
    ) -> None:
        active_revision = self.store.get_meta_int(
            connection,
            "active_canon_revision",
        )
        projection = connection.execute(
            """
            SELECT projection_hash
            FROM projection_runs
            WHERE projection_name='continuity'
              AND run_status='completed'
              AND source_active_revision=?
            ORDER BY created_at DESC, run_id DESC
            LIMIT 1
            """,
            (active_revision,),
        ).fetchone()
        actual_projection = (
            str(projection["projection_hash"])
            if projection is not None
            else ""
        )
        if (
            active_revision != int(row["prepared_canon_revision"])
            or actual_projection != str(row["active_projection_hash"])
        ):
            raise ExtractionJobConflict(
                "EXTRACTION_PREPARED_IDENTITY_STALE",
                "accepted canon revision or projection changed after "
                "Prepare",
                details={
                    "job_id": str(row["job_id"]),
                    "prepared_canon_revision": int(
                        row["prepared_canon_revision"]
                    ),
                    "active_canon_revision": active_revision,
                    "prepared_projection_hash": str(
                        row["active_projection_hash"]
                    ),
                    "active_projection_hash": actual_projection,
                },
            )

    @staticmethod
    def _proposal_payload(
        proposal: sqlite3.Row | Mapping[str, Any],
    ) -> dict[str, Any]:
        proposal_id = str(proposal["proposal_id"])
        try:
            payload = json.loads(str(proposal["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ExtractionJobConflict(
                "EXTRACTION_PROPOSAL_BINDING_INVALID",
                "proposal payload is not valid JSON",
                details={"proposal_id": proposal_id},
            ) from exc
        if not isinstance(payload, dict):
            raise ExtractionJobConflict(
                "EXTRACTION_PROPOSAL_BINDING_INVALID",
                "proposal payload must be an object",
                details={"proposal_id": proposal_id},
            )
        return payload

    @staticmethod
    def _shadow_configuration(
        job: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        artifact_context = job.get("artifact_context")
        if not isinstance(artifact_context, Mapping):
            return None
        value = artifact_context.get("_plot_rag_v15")
        if not isinstance(value, Mapping):
            return None
        configuration = dict(value)
        if str(
            configuration.get("extraction_execution_mode") or ""
        ).strip() != "async_shadow":
            return None
        return configuration

    @classmethod
    def _validate_proposal_identity(
        cls,
        connection: sqlite3.Connection,
        job_row: sqlite3.Row | Mapping[str, Any],
        proposal: sqlite3.Row | Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        proposal_id = str(proposal["proposal_id"])
        payload = cls._proposal_payload(proposal)
        decoded_job = cls._assert_persisted_binding(job_row)
        expected = cls._proposal_binding_for_job(decoded_job)
        binding_mismatches = {
            key: {
                "expected": value,
                "actual": payload.get(key),
            }
            for key, value in expected.items()
            if payload.get(key) != value
        }
        if binding_mismatches:
            raise ExtractionJobConflict(
                "EXTRACTION_PROPOSAL_BINDING_MISMATCH",
                "proposal does not carry the job's reverse binding",
                details={
                    "proposal_id": proposal_id,
                    "mismatches": binding_mismatches,
                },
            )
        artifact_context = expected["artifact_context"]
        expected_identity = {
            "artifact_id": str(artifact_context["artifact_id"]),
            "artifact_stage": str(artifact_context["artifact_stage"]),
            "branch_id": str(artifact_context["branch_id"]),
            "chapter_no": artifact_context.get("chapter_no"),
            "scene_index": artifact_context.get("scene_index"),
            "artifact_revision": int(
                artifact_context["artifact_revision"]
            ),
            "prepared_canon_revision": int(
                decoded_job["prepared_canon_revision"]
            ),
        }
        actual_identity = {
            "artifact_id": str(proposal["artifact_id"]),
            "artifact_stage": str(proposal["artifact_stage"]),
            "branch_id": str(proposal["branch_id"]),
            "chapter_no": (
                int(proposal["chapter_no"])
                if proposal["chapter_no"] is not None
                else None
            ),
            "scene_index": (
                int(proposal["scene_index"])
                if proposal["scene_index"] is not None
                else None
            ),
            "artifact_revision": int(proposal["artifact_revision"]),
            "prepared_canon_revision": int(
                proposal["prepared_canon_revision"]
            ),
        }
        identity_mismatches = {
            key: {
                "expected": expected_value,
                "actual": actual_identity[key],
            }
            for key, expected_value in expected_identity.items()
            if actual_identity[key] != expected_value
        }
        if identity_mismatches:
            raise ExtractionJobConflict(
                "EXTRACTION_PROPOSAL_IDENTITY_MISMATCH",
                "proposal artifact identity differs from its extraction job",
                details={
                    "proposal_id": proposal_id,
                    "mismatches": identity_mismatches,
                },
            )
        already_bound = connection.execute(
            """
            SELECT job_id
            FROM extraction_jobs
            WHERE result_proposal_id=? AND job_id<>?
            LIMIT 1
            """,
            (proposal_id, str(job_row["job_id"])),
        ).fetchone()
        if already_bound is not None:
            raise ExtractionJobConflict(
                "EXTRACTION_PROPOSAL_ALREADY_BOUND",
                "proposal is already bound to another extraction job",
                details={
                    "proposal_id": proposal_id,
                    "job_id": str(job_row["job_id"]),
                    "bound_job_id": str(already_bound["job_id"]),
                },
            )
        return decoded_job, payload

    @classmethod
    def _validate_result_proposal_status(
        cls,
        job: Mapping[str, Any],
        proposal: sqlite3.Row | Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> bool:
        proposal_id = str(proposal["proposal_id"])
        canon_status = str(proposal["canon_status"])
        shadow_configuration = cls._shadow_configuration(job)
        if shadow_configuration is None:
            if canon_status != "proposed":
                raise ExtractionJobConflict(
                    "EXTRACTION_PROPOSAL_STATUS_CONFLICT",
                    "result proposal must still be proposed when the worker "
                    "binds it",
                    details={
                        "proposal_id": proposal_id,
                        "canon_status": canon_status,
                    },
                )
            shadow_result = False
        else:
            if (
                canon_status != "rejected"
                or str(proposal["status_reason"])
                != "async_shadow_non_accepting"
            ):
                raise ExtractionJobConflict(
                    "EXTRACTION_PROPOSAL_STATUS_CONFLICT",
                    "async shadow results must be atomically rejected as "
                    "non-accepting proposals",
                    details={
                        "proposal_id": proposal_id,
                        "canon_status": canon_status,
                        "status_reason": str(proposal["status_reason"]),
                    },
                )
            expected_authoritative = _require_string(
                shadow_configuration.get("authoritative_proposal_id"),
                (
                    "artifact_context._plot_rag_v15."
                    "authoritative_proposal_id"
                ),
            )
            shadow_attestation = payload.get("extraction_shadow")
            actual_shadow = (
                dict(shadow_attestation)
                if isinstance(shadow_attestation, Mapping)
                else {}
            )
            expected_shadow = {
                "mode": "async_shadow",
                "authoritative_proposal_id": expected_authoritative,
                "acceptable": False,
                "barrier_blocking": False,
            }
            shadow_mismatches = {
                key: {
                    "expected": expected,
                    "actual": actual_shadow.get(key),
                }
                for key, expected in expected_shadow.items()
                if actual_shadow.get(key) != expected
            }
            if shadow_mismatches:
                raise ExtractionJobConflict(
                    "EXTRACTION_PROPOSAL_SHADOW_BINDING_MISMATCH",
                    "async shadow proposal lacks its non-authoritative "
                    "attestation",
                    details={
                        "proposal_id": proposal_id,
                        "mismatches": shadow_mismatches,
                    },
                )
            shadow_result = True
        if proposal["accepted_commit_id"] is not None:
            raise ExtractionJobConflict(
                "EXTRACTION_PROPOSAL_STATUS_CONFLICT",
                "worker result already references an accepted commit",
                details={
                    "proposal_id": proposal_id,
                    "accepted_commit_id": str(
                        proposal["accepted_commit_id"]
                    ),
                },
            )
        if str(proposal["validation_status"]) != "valid":
            raise ExtractionJobConflict(
                "EXTRACTION_PROPOSAL_VALIDATION_FAILED",
                "quarantined or invalid proposal cannot complete a job",
                details={
                    "proposal_id": proposal_id,
                    "validation_status": str(
                        proposal["validation_status"]
                    ),
                },
            )
        return shadow_result

    @classmethod
    def _validate_proposal_binding(
        cls,
        connection: sqlite3.Connection,
        job_row: sqlite3.Row | Mapping[str, Any],
        proposal_id: str,
    ) -> dict[str, Any]:
        proposal = connection.execute(
            """
            SELECT proposal_id, artifact_id, artifact_stage, branch_id,
                   chapter_no, scene_index, artifact_revision,
                   prepared_canon_revision, canon_status,
                   validation_status, status_reason, accepted_commit_id,
                   payload_json
            FROM proposals
            WHERE proposal_id=?
            """,
            (proposal_id,),
        ).fetchone()
        if proposal is None:
            raise ExtractionJobConflict(
                "EXTRACTION_PROPOSAL_NOT_FOUND",
                "result proposal does not exist",
                details={
                    "job_id": str(job_row["job_id"]),
                    "proposal_id": proposal_id,
                },
            )
        decoded_job, payload = cls._validate_proposal_identity(
            connection,
            job_row,
            proposal,
        )
        cls._validate_result_proposal_status(
            decoded_job,
            proposal,
            payload,
        )
        return payload

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        expected_attempt_count: int,
        lease_seconds: int = 60,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        job_id = _require_string(job_id, "job_id")
        worker = _require_string(worker_id, "worker_id")
        epoch = self._require_epoch(expected_attempt_count)
        seconds = _require_int(lease_seconds, "lease_seconds", minimum=1)
        assert seconds is not None
        with self.store.transaction() as connection:
            now_dt = self._now(now)
            timestamp = _format_timestamp(now_dt)
            expires = _format_timestamp(
                now_dt + timedelta(seconds=seconds)
            )
            current = self._leased_row(
                connection,
                job_id=job_id,
                worker_id=worker,
                expected_epoch=epoch,
                timestamp=timestamp,
            )
            heartbeat_at = max(
                timestamp,
                str(current["heartbeat_at"] or timestamp),
            )
            lease_expires_at = max(
                expires,
                str(current["lease_expires_at"]),
            )
            updated_at = max(
                timestamp,
                str(current["updated_at"] or timestamp),
            )
            cursor = connection.execute(
                """
                UPDATE extraction_jobs
                SET heartbeat_at=?, lease_expires_at=?, updated_at=?
                WHERE job_id=? AND job_status='running'
                  AND lease_owner=? AND attempt_count=?
                  AND lease_expires_at>?
                """,
                (
                    heartbeat_at,
                    lease_expires_at,
                    updated_at,
                    job_id,
                    worker,
                    epoch,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM extraction_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if cursor.rowcount != 1:
                raise self._lease_failure(job_id, worker, epoch, row)
        return self._decode_row(row)

    @staticmethod
    def _result_binding(
        *,
        result_kind: str | None,
        result_proposal_id: str | None,
    ) -> tuple[str, str | None]:
        proposal_id = (
            _require_string(result_proposal_id, "result_proposal_id")
            if result_proposal_id is not None
            else None
        )
        kind = (
            _require_string(result_kind, "result_kind")
            if result_kind is not None
            else ""
        )
        if not kind and proposal_id is not None:
            kind = "proposal"
        if not kind:
            raise ExtractionJobError(
                "EXTRACTION_RESULT_KIND_REQUIRED",
                "successful extraction without a proposal must explicitly "
                "attest result_kind=no_delta",
                details={"field": "result_kind"},
            )
        if kind not in RESULT_KINDS:
            raise ExtractionJobError(
                "EXTRACTION_RESULT_KIND_INVALID",
                f"unknown extraction result kind: {kind}",
                details={"result_kind": kind},
            )
        if kind == "proposal" and proposal_id is None:
            raise ExtractionJobError(
                "EXTRACTION_RESULT_BINDING_INVALID",
                "result_kind=proposal requires result_proposal_id",
            )
        if kind == "no_delta" and proposal_id is not None:
            raise ExtractionJobError(
                "EXTRACTION_RESULT_BINDING_INVALID",
                "result_kind=no_delta cannot bind a proposal",
            )
        return kind, proposal_id

    def succeed(
        self,
        job_id: str,
        *,
        worker_id: str,
        expected_attempt_count: int,
        validator_passed: bool,
        result_proposal_id: str | None = None,
        result_kind: str | None = None,
        remote_status: str = "validated",
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Mark a leased job successful after local validation."""

        if type(validator_passed) is not bool or not validator_passed:
            raise ExtractionJobError(
                "EXTRACTION_VALIDATION_REQUIRED",
                "a job may succeed only after the local validator passes",
            )
        job_id = _require_string(job_id, "job_id")
        worker = _require_string(worker_id, "worker_id")
        epoch = self._require_epoch(expected_attempt_count)
        kind, proposal_id = self._result_binding(
            result_kind=result_kind,
            result_proposal_id=result_proposal_id,
        )
        remote = _require_string(remote_status, "remote_status")
        with self.store.transaction() as connection:
            timestamp = _format_timestamp(self._now(now))
            leased = self._leased_row(
                connection,
                job_id=job_id,
                worker_id=worker,
                expected_epoch=epoch,
                timestamp=timestamp,
            )
            self._assert_prepared_identity(connection, leased)
            if kind == "proposal":
                assert proposal_id is not None
                self._validate_proposal_binding(
                    connection,
                    leased,
                    proposal_id,
                )
            try:
                cursor = connection.execute(
                    """
                    UPDATE extraction_jobs
                    SET job_status='succeeded',
                        remote_status=?,
                        result_kind=?,
                        result_proposal_id=?,
                        error='',
                        lease_owner='',
                        lease_expires_at=NULL,
                        heartbeat_at=?,
                        completed_at=?,
                        updated_at=?
                    WHERE job_id=? AND job_status='running'
                      AND lease_owner=? AND attempt_count=?
                      AND lease_expires_at>?
                    """,
                    (
                        remote,
                        kind,
                        proposal_id,
                        timestamp,
                        timestamp,
                        timestamp,
                        job_id,
                        worker,
                        epoch,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if proposal_id is not None:
                    raise ExtractionJobConflict(
                        "EXTRACTION_PROPOSAL_ALREADY_BOUND",
                        "proposal cannot be bound to this extraction job",
                        details={
                            "job_id": job_id,
                            "proposal_id": proposal_id,
                        },
                    ) from exc
                raise
            row = connection.execute(
                "SELECT * FROM extraction_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if cursor.rowcount != 1:
                raise self._lease_failure(job_id, worker, epoch, row)
            connection.execute(
                "DELETE FROM extraction_job_payloads WHERE job_id=?",
                (job_id,),
            )
        return self._decode_row(row)

    mark_succeeded = succeed

    def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        expected_attempt_count: int,
        error: str,
        remote_status: str = "failed",
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        job_id = _require_string(job_id, "job_id")
        worker = _require_string(worker_id, "worker_id")
        epoch = self._require_epoch(expected_attempt_count)
        error_text = _safe_error(_require_string(error, "error"))
        remote = _safe_error(
            _require_string(remote_status, "remote_status")
        )
        with self.store.transaction() as connection:
            timestamp = _format_timestamp(self._now(now))
            self._leased_row(
                connection,
                job_id=job_id,
                worker_id=worker,
                expected_epoch=epoch,
                timestamp=timestamp,
            )
            cursor = connection.execute(
                """
                UPDATE extraction_jobs
                SET job_status='failed',
                    remote_status=?,
                    error=?,
                    lease_owner='',
                    lease_expires_at=NULL,
                    heartbeat_at=?,
                    completed_at=?,
                    updated_at=?
                WHERE job_id=? AND job_status='running'
                  AND lease_owner=? AND attempt_count=?
                  AND lease_expires_at>?
                """,
                (
                    remote,
                    error_text,
                    timestamp,
                    timestamp,
                    timestamp,
                    job_id,
                    worker,
                    epoch,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM extraction_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if cursor.rowcount != 1:
                raise self._lease_failure(job_id, worker, epoch, row)
        return self._decode_row(row)

    mark_failed = fail

    @classmethod
    def _orphan_proposal_for_job(
        cls,
        connection: sqlite3.Connection,
        job_row: sqlite3.Row | Mapping[str, Any],
    ) -> str | None:
        job_id = str(job_row["job_id"])
        rows = connection.execute(
            """
            SELECT proposal_id, artifact_id, artifact_stage, branch_id,
                   chapter_no, scene_index, artifact_revision,
                   prepared_canon_revision, canon_status,
                   validation_status, status_reason, accepted_commit_id,
                   payload_json, created_at
            FROM proposals
            WHERE instr(payload_json, ?) > 0
            ORDER BY created_at, proposal_id
            """,
            (job_id,),
        ).fetchall()
        valid_candidates: list[str] = []
        for proposal in rows:
            try:
                payload = cls._proposal_payload(proposal)
            except ExtractionJobError as exc:
                raise ExtractionJobConflict(
                    "EXTRACTION_ORPHAN_PROPOSAL_INVALID",
                    "a possible orphan proposal has a corrupt payload",
                    details={
                        "job_id": job_id,
                        "proposal_id": str(proposal["proposal_id"]),
                        "source_code": exc.code,
                    },
                ) from exc
            if payload.get("extraction_job_id") != job_id:
                continue
            try:
                decoded_job, validated_payload = (
                    cls._validate_proposal_identity(
                        connection,
                        job_row,
                        proposal,
                    )
                )
                cls._validate_result_proposal_status(
                    decoded_job,
                    proposal,
                    validated_payload,
                )
            except ExtractionJobError as exc:
                raise ExtractionJobConflict(
                    "EXTRACTION_ORPHAN_PROPOSAL_INVALID",
                    "reverse-bound orphan proposal failed validation",
                    details={
                        "job_id": job_id,
                        "proposal_id": str(proposal["proposal_id"]),
                        "source_code": exc.code,
                    },
                ) from exc
            valid_candidates.append(str(proposal["proposal_id"]))
        if len(valid_candidates) > 1:
            raise ExtractionJobConflict(
                "EXTRACTION_ORPHAN_PROPOSAL_AMBIGUOUS",
                "multiple valid orphan proposals bind the same extraction job",
                details={
                    "job_id": job_id,
                    "proposal_ids": valid_candidates,
                },
            )
        return valid_candidates[0] if valid_candidates else None

    def _claimed_work_snapshot(
        self,
        *,
        job_id: str,
        worker_id: str,
        expected_epoch: int,
        now: str | datetime | None,
    ) -> tuple[dict[str, Any], str | None, str | None]:
        with self.store.transaction() as connection:
            timestamp = _format_timestamp(self._now(now))
            row = self._leased_row(
                connection,
                job_id=job_id,
                worker_id=worker_id,
                expected_epoch=expected_epoch,
                timestamp=timestamp,
            )
            self._assert_prepared_identity(connection, row)
            orphan_proposal_id = self._orphan_proposal_for_job(
                connection,
                row,
            )
            decoded = self._decode_row(row)
            if orphan_proposal_id is not None:
                return decoded, None, orphan_proposal_id
            payload = connection.execute(
                """
                SELECT assistant_text, assistant_sha256, payload_bytes
                FROM extraction_job_payloads
                WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
            assistant_text = self._validated_payload_text(row, payload)
            return decoded, assistant_text, None

    def _start_heartbeat_thread(
        self,
        *,
        job_id: str,
        worker_id: str,
        expected_epoch: int,
        lease_seconds: int,
        heartbeat_interval_seconds: float,
    ) -> tuple[
        threading.Event,
        threading.Thread,
        list[BaseException],
    ]:
        stop = threading.Event()
        errors: list[BaseException] = []

        def heartbeat_loop() -> None:
            while not stop.wait(heartbeat_interval_seconds):
                try:
                    self.heartbeat(
                        job_id,
                        worker_id=worker_id,
                        expected_attempt_count=expected_epoch,
                        lease_seconds=lease_seconds,
                    )
                except BaseException as exc:
                    errors.append(exc)
                    stop.set()
                    return

        thread = threading.Thread(
            target=heartbeat_loop,
            name=f"plot-rag-extraction-heartbeat-{job_id}",
            daemon=True,
        )
        thread.start()
        return stop, thread, errors

    @staticmethod
    def _heartbeat_failure(
        *,
        job_id: str,
        worker_id: str,
        expected_epoch: int,
        error: BaseException,
    ) -> ExtractionLeaseLost:
        if isinstance(error, ExtractionLeaseLost):
            return error
        return ExtractionLeaseLost(
            "EXTRACTION_JOB_HEARTBEAT_FAILED",
            "automatic lease heartbeat failed; worker result is fenced",
            details={
                "job_id": job_id,
                "worker_id": worker_id,
                "expected_attempt_count": expected_epoch,
                "heartbeat_error": _safe_error(error),
            },
        )

    @staticmethod
    def _work_result(
        value: ExtractionWorkResult | Mapping[str, Any],
    ) -> ExtractionWorkResult:
        if isinstance(value, ExtractionWorkResult):
            validator_passed = value.validator_passed
            if type(validator_passed) is not bool:
                raise ExtractionJobError(
                    "EXTRACTION_WORK_RESULT_INVALID",
                    "proposal_factory result requires boolean "
                    "validator_passed",
                    details={"field": "validator_passed"},
                )
            kind, proposal_id = ExtractionJobQueue._result_binding(
                result_kind=value.result_kind,
                result_proposal_id=value.result_proposal_id,
            ) if validator_passed else (
                value.result_kind or "",
                value.result_proposal_id,
            )
            return ExtractionWorkResult(
                validator_passed=validator_passed,
                result_proposal_id=proposal_id,
                result_kind=kind or None,
                remote_status=value.remote_status,
                error=value.error,
            )
        if not isinstance(value, Mapping):
            raise ExtractionJobError(
                "EXTRACTION_WORK_RESULT_INVALID",
                "proposal_factory must return ExtractionWorkResult or a "
                "mapping",
            )
        validator_passed = value.get("validator_passed")
        if type(validator_passed) is not bool:
            raise ExtractionJobError(
                "EXTRACTION_WORK_RESULT_INVALID",
                "proposal_factory result requires boolean validator_passed",
                details={"field": "validator_passed"},
            )
        proposal_id = value.get(
            "result_proposal_id",
            value.get("proposal_id"),
        )
        if proposal_id is not None:
            proposal_id = _require_string(
                proposal_id,
                "result_proposal_id",
            )
        result_kind = value.get("result_kind")
        if result_kind is not None:
            result_kind = _require_string(result_kind, "result_kind")
        if validator_passed:
            result_kind, proposal_id = ExtractionJobQueue._result_binding(
                result_kind=result_kind,
                result_proposal_id=proposal_id,
            )
        remote_status = str(
            value.get("remote_status")
            or ("validated" if validator_passed else "validation_failed")
        )
        error = str(value.get("error") or "")
        return ExtractionWorkResult(
            validator_passed=validator_passed,
            result_proposal_id=proposal_id,
            result_kind=result_kind,
            remote_status=remote_status,
            error=error,
        )

    def process_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        expected_attempt_count: int,
        proposal_factory: Callable[
            [dict[str, Any], str],
            ExtractionWorkResult | Mapping[str, Any],
        ],
        lease_seconds: int = 60,
        heartbeat_interval_seconds: float | None = None,
        now: str | datetime | None = None,
        raise_on_error: bool = False,
    ) -> dict[str, Any]:
        """Run one already-claimed job through a proposal-only callback.

        The callback receives metadata and the separately loaded assistant
        text.  It is responsible for remote extraction, deterministic repair,
        local validation, and durable proposal creation.  Its return value can
        only bind that proposal to the job; this helper never grants or accepts
        the proposal.
        """

        job_id = _require_string(job_id, "job_id")
        worker = _require_string(worker_id, "worker_id")
        epoch = self._require_epoch(expected_attempt_count)
        if not callable(proposal_factory):
            raise ExtractionJobError(
                "EXTRACTION_JOB_INVALID_ARGUMENT",
                "proposal_factory must be callable",
                details={"field": "proposal_factory"},
            )
        checked_lease_seconds = _require_int(
            lease_seconds,
            "lease_seconds",
            minimum=1,
        )
        assert checked_lease_seconds is not None
        interval = (
            _require_positive_seconds(
                heartbeat_interval_seconds,
                "heartbeat_interval_seconds",
            )
            if heartbeat_interval_seconds is not None
            else max(0.1, checked_lease_seconds / 3.0)
        )
        if interval >= checked_lease_seconds:
            raise ExtractionJobError(
                "EXTRACTION_JOB_INVALID_ARGUMENT",
                "heartbeat_interval_seconds must be shorter than the lease",
                details={
                    "heartbeat_interval_seconds": interval,
                    "lease_seconds": checked_lease_seconds,
                },
            )
        if not isinstance(raise_on_error, bool):
            raise ExtractionJobError(
                "EXTRACTION_JOB_INVALID_ARGUMENT",
                "raise_on_error must be boolean",
                details={"field": "raise_on_error"},
            )

        processing_started = time.perf_counter()
        explicit_base_now = self._now(now) if now is not None else None

        def completion_now() -> datetime | None:
            if explicit_base_now is None:
                return None
            elapsed = max(0.0, time.perf_counter() - processing_started)
            return explicit_base_now + timedelta(seconds=elapsed)

        try:
            job, assistant_text, orphan_proposal_id = (
                self._claimed_work_snapshot(
                    job_id=job_id,
                    worker_id=worker,
                    expected_epoch=epoch,
                    now=now,
                )
            )
            if orphan_proposal_id is not None:
                succeeded = self.succeed(
                    job_id,
                    worker_id=worker,
                    expected_attempt_count=epoch,
                    validator_passed=True,
                    result_proposal_id=orphan_proposal_id,
                    result_kind="proposal",
                    remote_status="adopted_orphan",
                    now=completion_now(),
                )
                return {
                    "status": "succeeded",
                    "job": succeeded,
                    "proposal_id": orphan_proposal_id,
                    "adopted": True,
                    "error": "",
                    "processing_ms": (
                        time.perf_counter() - processing_started
                    )
                    * 1000.0,
                }
            if assistant_text is None:
                raise ExtractionJobError(
                    "EXTRACTION_JOB_PAYLOAD_MISSING",
                    "claimed job has no assistant payload or orphan proposal",
                    details={"job_id": job_id},
                )
            heartbeat_state = (
                self._start_heartbeat_thread(
                    job_id=job_id,
                    worker_id=worker,
                    expected_epoch=epoch,
                    lease_seconds=checked_lease_seconds,
                    heartbeat_interval_seconds=interval,
                )
                if now is None
                else None
            )
            try:
                raw_result = proposal_factory(job, assistant_text)
            finally:
                if heartbeat_state is not None:
                    heartbeat_stop, heartbeat_thread, _ = heartbeat_state
                    heartbeat_stop.set()
                    heartbeat_thread.join(
                        timeout=max(1.0, interval * 2.0)
                    )
                    if heartbeat_thread.is_alive():
                        heartbeat_state[2].append(
                            ExtractionLeaseLost(
                                "EXTRACTION_JOB_HEARTBEAT_FAILED",
                                "automatic heartbeat thread did not stop "
                                "within its bounded join timeout",
                                details={
                                    "job_id": job_id,
                                    "worker_id": worker,
                                    "expected_attempt_count": epoch,
                                    "heartbeat_thread_alive": True,
                                },
                            )
                        )
            if heartbeat_state is not None and heartbeat_state[2]:
                raise self._heartbeat_failure(
                    job_id=job_id,
                    worker_id=worker,
                    expected_epoch=epoch,
                    error=heartbeat_state[2][0],
                )
            work_result = self._work_result(raw_result)
            if not work_result.validator_passed:
                error = _safe_error(
                    work_result.error or "local validator rejected proposal"
                )
                failed = self.fail(
                    job_id,
                    worker_id=worker,
                    expected_attempt_count=epoch,
                    error=error,
                    remote_status=(
                        work_result.remote_status or "validation_failed"
                    ),
                    now=completion_now(),
                )
                return {
                    "status": "failed",
                    "job": failed,
                    "proposal_id": None,
                    "error": error,
                    "processing_ms": (
                        time.perf_counter() - processing_started
                    )
                    * 1000.0,
                }
            succeeded = self.succeed(
                job_id,
                worker_id=worker,
                expected_attempt_count=epoch,
                validator_passed=True,
                result_proposal_id=work_result.result_proposal_id,
                result_kind=work_result.result_kind,
                remote_status=work_result.remote_status,
                now=completion_now(),
            )
            return {
                "status": "succeeded",
                "job": succeeded,
                "proposal_id": work_result.result_proposal_id,
                "adopted": False,
                "error": "",
                "processing_ms": (
                    time.perf_counter() - processing_started
                )
                * 1000.0,
            }
        except ExtractionLeaseLost as exc:
            if raise_on_error:
                raise
            return {
                "status": "lease_lost",
                "job": self.inspect(job_id),
                "proposal_id": None,
                "error": _safe_error(exc),
                "processing_ms": (
                    time.perf_counter() - processing_started
                )
                * 1000.0,
            }
        except Exception as exc:
            error = _safe_error(exc)
            try:
                failed = self.fail(
                    job_id,
                    worker_id=worker,
                    expected_attempt_count=epoch,
                    error=error,
                    remote_status="processor_error",
                    now=completion_now(),
                )
            except ExtractionLeaseLost:
                if raise_on_error:
                    raise
                return {
                    "status": "lease_lost",
                    "job": self.inspect(job_id),
                    "proposal_id": None,
                    "error": error,
                    "processing_ms": (
                        time.perf_counter() - processing_started
                    )
                    * 1000.0,
                }
            if raise_on_error:
                raise
            return {
                "status": "failed",
                "job": failed,
                "proposal_id": None,
                "error": error,
                "processing_ms": (
                    time.perf_counter() - processing_started
                )
                * 1000.0,
            }

    def run_once(
        self,
        *,
        worker_id: str,
        proposal_factory: Callable[
            [dict[str, Any], str],
            ExtractionWorkResult | Mapping[str, Any],
        ],
        lease_seconds: int = 60,
        heartbeat_interval_seconds: float | None = None,
        branch_id: str | None = None,
        recover_stale: bool = True,
        now: str | datetime | None = None,
        raise_on_error: bool = False,
    ) -> dict[str, Any]:
        """Recover stale leases, claim at most one job, and process it."""

        if not isinstance(recover_stale, bool):
            raise ExtractionJobError(
                "EXTRACTION_JOB_INVALID_ARGUMENT",
                "recover_stale must be boolean",
                details={"field": "recover_stale"},
            )
        checked_lease_seconds = _require_int(
            lease_seconds,
            "lease_seconds",
            minimum=1,
        )
        assert checked_lease_seconds is not None
        if heartbeat_interval_seconds is not None:
            checked_interval = _require_positive_seconds(
                heartbeat_interval_seconds,
                "heartbeat_interval_seconds",
            )
            if checked_interval >= checked_lease_seconds:
                raise ExtractionJobError(
                    "EXTRACTION_JOB_INVALID_ARGUMENT",
                    "heartbeat_interval_seconds must be shorter than the "
                    "lease",
                    details={
                        "heartbeat_interval_seconds": checked_interval,
                        "lease_seconds": checked_lease_seconds,
                    },
                )
        recovered = (
            self.recover_stale_running(now=now)
            if recover_stale
            else []
        )
        claimed = self.claim(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            branch_id=branch_id,
            now=now,
        )
        if claimed is None:
            return {
                "status": "idle",
                "job": None,
                "proposal_id": None,
                "error": "",
                "recovered_job_ids": [
                    str(job["job_id"]) for job in recovered
                ],
                "processing_ms": 0.0,
            }
        result = self.process_job(
            str(claimed["job_id"]),
            worker_id=worker_id,
            expected_attempt_count=int(claimed["attempt_count"]),
            proposal_factory=proposal_factory,
            lease_seconds=lease_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            now=now,
            raise_on_error=raise_on_error,
        )
        result["recovered_job_ids"] = [
            str(job["job_id"]) for job in recovered
        ]
        return result

    process_claimed_job = process_job
    worker_run_once = run_once

    def retry(
        self,
        job_id: str,
        *,
        expected_attempt_count: int,
        next_attempt_at: str | datetime | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """CAS a failed job back to queued without resetting its epoch."""

        job_id = _require_string(job_id, "job_id")
        epoch = _require_int(
            expected_attempt_count,
            "expected_attempt_count",
        )
        assert epoch is not None
        due = (
            _format_timestamp(
                _parse_timestamp(next_attempt_at, "next_attempt_at")
            )
            if next_attempt_at is not None
            else None
        )
        with self.store.transaction() as connection:
            timestamp = _format_timestamp(self._now(now))
            current = connection.execute(
                "SELECT * FROM extraction_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if current is None:
                raise ExtractionJobNotFound(
                    "EXTRACTION_JOB_NOT_FOUND",
                    f"no extraction job named {job_id}",
                    details={"job_id": job_id},
                )
            self._assert_persisted_binding(current)
            if (
                str(current["job_status"]) != "failed"
                or int(current["attempt_count"]) != epoch
            ):
                raise ExtractionJobConflict(
                    "EXTRACTION_JOB_RETRY_CONFLICT",
                    "retry requires the expected failed job epoch",
                    details={
                        "job_id": job_id,
                        "expected_attempt_count": epoch,
                        "actual_status": str(current["job_status"]),
                        "actual_attempt_count": int(
                            current["attempt_count"]
                        ),
                    },
                )
            payload = connection.execute(
                """
                SELECT 1
                FROM extraction_job_payloads
                WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
            if payload is None:
                raise ExtractionJobError(
                    "EXTRACTION_JOB_PAYLOAD_MISSING",
                    "failed job cannot be retried without its durable "
                    "assistant payload",
                    details={"job_id": job_id},
                )
            cursor = connection.execute(
                """
                UPDATE extraction_jobs
                SET job_status='queued',
                    remote_status='retry_queued',
                    result_kind='',
                    result_proposal_id=NULL,
                    error='',
                    lease_owner='',
                    lease_expires_at=NULL,
                    heartbeat_at=NULL,
                    next_attempt_at=?,
                    started_at=NULL,
                    completed_at=NULL,
                    updated_at=?
                WHERE job_id=? AND job_status='failed'
                  AND attempt_count=?
                """,
                (due, timestamp, job_id, epoch),
            )
            row = connection.execute(
                "SELECT * FROM extraction_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ExtractionJobNotFound(
                    "EXTRACTION_JOB_NOT_FOUND",
                    f"no extraction job named {job_id}",
                    details={"job_id": job_id},
                )
            if cursor.rowcount != 1:
                raise ExtractionJobConflict(
                    "EXTRACTION_JOB_RETRY_CONFLICT",
                    "retry requires the expected failed job epoch",
                    details={
                        "job_id": job_id,
                        "expected_attempt_count": epoch,
                        "actual_status": str(row["job_status"]),
                        "actual_attempt_count": int(row["attempt_count"]),
                    },
                )
        return self._decode_row(row)

    retry_job = retry

    def cancel(
        self,
        job_id: str,
        *,
        expected_attempt_count: int,
        reason: str,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """CAS a non-successful job to the terminal cancelled state."""

        job_id = _require_string(job_id, "job_id")
        epoch = _require_int(
            expected_attempt_count,
            "expected_attempt_count",
        )
        assert epoch is not None
        reason_text = _require_string(reason, "reason")
        with self.store.transaction() as connection:
            timestamp = _format_timestamp(self._now(now))
            current = connection.execute(
                "SELECT * FROM extraction_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if current is None:
                raise ExtractionJobNotFound(
                    "EXTRACTION_JOB_NOT_FOUND",
                    f"no extraction job named {job_id}",
                    details={"job_id": job_id},
                )
            self._assert_persisted_binding(current)
            cursor = connection.execute(
                """
                UPDATE extraction_jobs
                SET job_status='cancelled',
                    remote_status='cancelled',
                    error=?,
                    lease_owner='',
                    lease_expires_at=NULL,
                    heartbeat_at=NULL,
                    next_attempt_at=NULL,
                    completed_at=?,
                    updated_at=?
                WHERE job_id=? AND job_status IN ('queued', 'running', 'failed')
                  AND attempt_count=?
                """,
                (reason_text, timestamp, timestamp, job_id, epoch),
            )
            row = connection.execute(
                "SELECT * FROM extraction_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ExtractionJobNotFound(
                    "EXTRACTION_JOB_NOT_FOUND",
                    f"no extraction job named {job_id}",
                    details={"job_id": job_id},
                )
            if cursor.rowcount != 1:
                raise ExtractionJobConflict(
                    "EXTRACTION_JOB_CANCEL_CONFLICT",
                    "cancel requires the expected non-successful job epoch",
                    details={
                        "job_id": job_id,
                        "expected_attempt_count": epoch,
                        "actual_status": str(row["job_status"]),
                        "actual_attempt_count": int(row["attempt_count"]),
                    },
                )
            connection.execute(
                "DELETE FROM extraction_job_payloads WHERE job_id=?",
                (job_id,),
            )
        return self._decode_row(row)

    cancel_job = cancel

    @staticmethod
    def _resolution_binding_payload(
        *,
        job_id: str,
        branch_id: str,
        sequence_no: int,
        expected_attempt_count: int,
        action: str,
        replacement_job_id: str | None,
        target_branch_id: str,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "branch_id": branch_id,
            "sequence_no": sequence_no,
            "expected_attempt_count": expected_attempt_count,
            "action": action,
            "replacement_job_id": replacement_job_id,
            "target_branch_id": target_branch_id,
            "reason": reason,
        }

    @classmethod
    def _decode_resolution(
        cls,
        row: sqlite3.Row | Mapping[str, Any],
    ) -> dict[str, Any]:
        raw = dict(row)
        payload = cls._resolution_binding_payload(
            job_id=str(raw["job_id"]),
            branch_id=str(raw["branch_id"]),
            sequence_no=int(raw["sequence_no"]),
            expected_attempt_count=int(raw["expected_attempt_count"]),
            action=str(raw["action"]),
            replacement_job_id=(
                str(raw["replacement_job_id"])
                if raw["replacement_job_id"] is not None
                else None
            ),
            target_branch_id=str(raw["target_branch_id"]),
            reason=str(raw["reason"]),
        )
        expected_hash = _sha256_text(
            _canonical_json(payload, "barrier_resolution")
        )
        if expected_hash != str(raw["binding_hash"]):
            raise ExtractionJobError(
                "EXTRACTION_BARRIER_RESOLUTION_CORRUPT",
                "persisted barrier resolution binding hash does not match",
                details={
                    "resolution_id": str(raw["resolution_id"]),
                    "job_id": str(raw["job_id"]),
                },
            )
        raw["binding_hash"] = expected_hash
        raw["reused"] = bool(raw.get("reused", False))
        return raw

    def resolve_barrier(
        self,
        job_id: str,
        *,
        expected_attempt_count: int,
        action: str,
        reason: str,
        replacement_job_id: str | None = None,
        target_branch_id: str | None = None,
        resolution_id: str | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Durably resolve a cancelled/rejected/retracted story barrier."""

        job_id = _require_string(job_id, "job_id")
        epoch = _require_int(
            expected_attempt_count,
            "expected_attempt_count",
        )
        assert epoch is not None
        checked_action = _require_string(action, "action")
        if checked_action not in BARRIER_RESOLUTION_ACTIONS:
            raise ExtractionJobError(
                "EXTRACTION_BARRIER_RESOLUTION_INVALID",
                f"unknown barrier resolution action: {checked_action}",
                details={"action": checked_action},
            )
        reason_text = _require_string(reason, "reason")
        replacement = (
            _require_string(replacement_job_id, "replacement_job_id")
            if replacement_job_id is not None
            else None
        )
        target_branch = (
            _require_string(target_branch_id, "target_branch_id")
            if target_branch_id is not None
            else ""
        )
        requested_resolution_id = (
            _require_string(resolution_id, "resolution_id")
            if resolution_id is not None
            else f"extract-resolution-{uuid.uuid4().hex}"
        )
        if checked_action in {"rewrite", "supersede"} and replacement is None:
            raise ExtractionJobError(
                "EXTRACTION_BARRIER_RESOLUTION_INVALID",
                f"{checked_action} requires replacement_job_id",
            )
        if checked_action == "branch_switch" and not target_branch:
            raise ExtractionJobError(
                "EXTRACTION_BARRIER_RESOLUTION_INVALID",
                "branch_switch requires target_branch_id",
            )
        if checked_action == "branch_switch" and replacement is not None:
            raise ExtractionJobError(
                "EXTRACTION_BARRIER_RESOLUTION_INVALID",
                "branch_switch cannot bind a replacement job",
            )
        if checked_action in {"rewrite", "supersede"} and target_branch:
            raise ExtractionJobError(
                "EXTRACTION_BARRIER_RESOLUTION_INVALID",
                f"{checked_action} cannot bind target_branch_id",
            )
        if checked_action == "discard" and (
            replacement is not None or target_branch
        ):
            raise ExtractionJobError(
                "EXTRACTION_BARRIER_RESOLUTION_INVALID",
                "discard cannot bind a replacement job or target branch",
            )

        with self.store.transaction() as connection:
            timestamp = _format_timestamp(self._now(now))
            job = connection.execute(
                "SELECT * FROM extraction_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise ExtractionJobNotFound(
                    "EXTRACTION_JOB_NOT_FOUND",
                    f"no extraction job named {job_id}",
                    details={"job_id": job_id},
                )
            self._assert_persisted_binding(job)
            if int(job["attempt_count"]) != epoch:
                raise ExtractionJobConflict(
                    "EXTRACTION_BARRIER_RESOLUTION_CONFLICT",
                    "barrier resolution requires the current job epoch",
                    details={
                        "job_id": job_id,
                        "expected_attempt_count": epoch,
                        "actual_attempt_count": int(job["attempt_count"]),
                    },
                )
            if job["sequence_no"] is None:
                raise ExtractionJobConflict(
                    "EXTRACTION_SEQUENCE_REQUIRED",
                    "numbered story barrier resolution requires sequence_no",
                    details={"job_id": job_id},
                )
            branch = str(job["branch_id"])
            sequence = int(job["sequence_no"])
            eligible = str(job["job_status"]) == "cancelled"
            disposition = str(job["job_status"])
            if str(job["job_status"]) == "succeeded":
                proposal_id = job["result_proposal_id"]
                proposal = (
                    connection.execute(
                        """
                        SELECT canon_status
                        FROM proposals
                        WHERE proposal_id=?
                        """,
                        (str(proposal_id),),
                    ).fetchone()
                    if proposal_id is not None
                    else None
                )
                disposition = (
                    str(proposal["canon_status"])
                    if proposal is not None
                    else "missing"
                )
                eligible = disposition in {"rejected", "retracted"}
            if not eligible:
                raise ExtractionJobConflict(
                    "EXTRACTION_BARRIER_RESOLUTION_REQUIRED",
                    "only cancelled, rejected, or retracted barriers can be "
                    "explicitly resolved",
                    details={
                        "job_id": job_id,
                        "job_status": str(job["job_status"]),
                        "disposition": disposition,
                    },
                )

            if replacement is not None:
                if replacement == job_id:
                    raise ExtractionJobError(
                        "EXTRACTION_BARRIER_RESOLUTION_INVALID",
                        "replacement job must differ from the resolved job",
                    )
                replacement_row = connection.execute(
                    "SELECT * FROM extraction_jobs WHERE job_id=?",
                    (replacement,),
                ).fetchone()
                if replacement_row is None:
                    raise ExtractionJobConflict(
                        "EXTRACTION_REPLACEMENT_JOB_NOT_FOUND",
                        "replacement extraction job does not exist",
                        details={"replacement_job_id": replacement},
                    )
                self._assert_persisted_binding(replacement_row)
                if (
                    str(replacement_row["branch_id"]) != branch
                    or replacement_row["sequence_no"] is None
                    or int(replacement_row["sequence_no"]) != sequence
                ):
                    raise ExtractionJobConflict(
                        "EXTRACTION_REPLACEMENT_SCOPE_MISMATCH",
                        "replacement job must share branch and sequence",
                        details={
                            "job_id": job_id,
                            "replacement_job_id": replacement,
                        },
                    )
            if checked_action == "branch_switch" and target_branch == branch:
                raise ExtractionJobError(
                    "EXTRACTION_BARRIER_RESOLUTION_INVALID",
                    "target branch must differ from the blocked branch",
                )

            binding_payload = self._resolution_binding_payload(
                job_id=job_id,
                branch_id=branch,
                sequence_no=sequence,
                expected_attempt_count=epoch,
                action=checked_action,
                replacement_job_id=replacement,
                target_branch_id=target_branch,
                reason=reason_text,
            )
            binding_hash = _sha256_text(
                _canonical_json(
                    binding_payload,
                    "barrier_resolution",
                )
            )
            existing = connection.execute(
                """
                SELECT *
                FROM extraction_barrier_resolutions
                WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
            if existing is not None:
                decoded = self._decode_resolution(existing)
                if decoded["binding_hash"] != binding_hash:
                    raise ExtractionJobConflict(
                        "EXTRACTION_BARRIER_RESOLUTION_CONFLICT",
                        "job already has a different barrier resolution",
                        details={
                            "job_id": job_id,
                            "resolution_id": str(
                                existing["resolution_id"]
                            ),
                        },
                    )
                decoded["reused"] = True
                return decoded
            connection.execute(
                """
                INSERT INTO extraction_barrier_resolutions(
                    resolution_id, job_id, branch_id, sequence_no,
                    expected_attempt_count, action, replacement_job_id,
                    target_branch_id, reason, binding_hash, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    requested_resolution_id,
                    job_id,
                    branch,
                    sequence,
                    epoch,
                    checked_action,
                    replacement,
                    target_branch,
                    reason_text,
                    binding_hash,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT *
                FROM extraction_barrier_resolutions
                WHERE resolution_id=?
                """,
                (requested_resolution_id,),
            ).fetchone()
        if row is None:
            raise ExtractionJobError(
                "EXTRACTION_BARRIER_RESOLUTION_PERSIST_FAILED",
                "barrier resolution disappeared during persistence",
            )
        return self._decode_resolution(row)

    resolve = resolve_barrier

    def recover_stale_running(
        self,
        *,
        now: str | datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return expired running leases to queued using per-row CAS."""

        checked_limit = _require_int(limit, "limit", minimum=1)
        assert checked_limit is not None
        if checked_limit > _MAX_LIST_LIMIT:
            raise ExtractionJobError(
                "EXTRACTION_JOB_INVALID_INTEGER",
                f"limit must be <= {_MAX_LIST_LIMIT}",
                details={"field": "limit", "maximum": _MAX_LIST_LIMIT},
            )
        recovered_ids: list[str] = []
        with self.store.transaction() as connection:
            timestamp = _format_timestamp(self._now(now))
            stale = connection.execute(
                """
                SELECT *
                FROM extraction_jobs
                WHERE job_status='running'
                  AND (lease_expires_at IS NULL OR lease_expires_at<=?)
                ORDER BY lease_expires_at, updated_at, job_id
                LIMIT ?
                """,
                (timestamp, checked_limit),
            ).fetchall()
            for candidate in stale:
                self._assert_persisted_binding(candidate)
                cursor = connection.execute(
                    """
                    UPDATE extraction_jobs
                    SET job_status='queued',
                        remote_status='stale_lease_recovered',
                        error='STALE_RUNNING_RECOVERED',
                        lease_owner='',
                        lease_expires_at=NULL,
                        heartbeat_at=NULL,
                        next_attempt_at=NULL,
                        started_at=NULL,
                        completed_at=NULL,
                        updated_at=?
                    WHERE job_id=? AND job_status='running'
                      AND attempt_count=?
                      AND (lease_expires_at IS NULL OR lease_expires_at<=?)
                    """,
                    (
                        timestamp,
                        str(candidate["job_id"]),
                        int(candidate["attempt_count"]),
                        timestamp,
                    ),
                )
                if cursor.rowcount == 1:
                    recovered_ids.append(str(candidate["job_id"]))
            if recovered_ids:
                placeholders = ",".join("?" for _ in recovered_ids)
                rows = connection.execute(
                    f"""
                    SELECT *
                    FROM extraction_jobs
                    WHERE job_id IN ({placeholders})
                    ORDER BY updated_at, job_id
                    """,
                    recovered_ids,
                ).fetchall()
            else:
                rows = []
        return [self._decode_row(row) for row in rows]

    recover_stale = recover_stale_running

    @staticmethod
    def _accepted_commit_identity(
        proposal: sqlite3.Row | Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "artifact_id": str(proposal["artifact_id"]),
            "artifact_stage": str(proposal["artifact_stage"]),
            "branch_id": str(proposal["branch_id"]),
            "chapter_no": (
                int(proposal["chapter_no"])
                if proposal["chapter_no"] is not None
                else None
            ),
            "scene_index": (
                int(proposal["scene_index"])
                if proposal["scene_index"] is not None
                else None
            ),
            "artifact_revision": int(proposal["artifact_revision"]),
        }

    @classmethod
    def _validate_accepted_barrier_commit(
        cls,
        proposal: sqlite3.Row | Mapping[str, Any],
        commit: sqlite3.Row | Mapping[str, Any] | None,
    ) -> None:
        proposal_id = str(proposal["proposal_id"])
        if str(proposal["validation_status"]) != "valid":
            raise ExtractionJobConflict(
                "EXTRACTION_ACCEPTED_PROPOSAL_INVALID",
                "accepted proposal is not locally valid",
                details={
                    "proposal_id": proposal_id,
                    "validation_status": str(
                        proposal["validation_status"]
                    ),
                },
            )
        accepted_commit_id = str(
            proposal["accepted_commit_id"] or ""
        ).strip()
        if not accepted_commit_id:
            raise ExtractionJobConflict(
                "EXTRACTION_ACCEPTED_COMMIT_MISSING",
                "accepted proposal has no accepted_commit_id",
                details={"proposal_id": proposal_id},
            )
        if commit is None:
            raise ExtractionJobConflict(
                "EXTRACTION_ACCEPTED_COMMIT_NOT_FOUND",
                "accepted proposal references a missing canon commit",
                details={
                    "proposal_id": proposal_id,
                    "accepted_commit_id": accepted_commit_id,
                },
            )
        if (
            str(commit["commit_id"]) != accepted_commit_id
            or str(commit["operation"]) != "accept"
            or str(commit["proposal_id"]) != proposal_id
        ):
            raise ExtractionJobConflict(
                "EXTRACTION_ACCEPTED_COMMIT_MISMATCH",
                "accepted canon commit does not bind this proposal",
                details={
                    "proposal_id": proposal_id,
                    "accepted_commit_id": accepted_commit_id,
                    "commit_id": str(commit["commit_id"]),
                    "commit_operation": str(commit["operation"]),
                    "commit_proposal_id": str(commit["proposal_id"]),
                },
            )
        expected_identity = cls._accepted_commit_identity(proposal)
        actual_identity = cls._accepted_commit_identity(commit)
        mismatches = {
            key: {
                "expected": expected,
                "actual": actual_identity[key],
            }
            for key, expected in expected_identity.items()
            if actual_identity[key] != expected
        }
        if mismatches:
            raise ExtractionJobConflict(
                "EXTRACTION_ACCEPTED_COMMIT_IDENTITY_MISMATCH",
                "accepted canon commit artifact identity differs from the "
                "proposal",
                details={
                    "proposal_id": proposal_id,
                    "accepted_commit_id": accepted_commit_id,
                    "mismatches": mismatches,
                },
            )

    @staticmethod
    def _proposal_summary(
        row: sqlite3.Row | Mapping[str, Any] | None,
        proposal_id: str,
    ) -> dict[str, Any]:
        if row is None:
            return {
                "proposal_id": proposal_id,
                "canon_status": "missing",
                "missing": True,
            }
        raw = dict(row)
        return {
            "proposal_id": str(raw["proposal_id"]),
            "canon_status": str(raw["canon_status"]),
            "validation_status": str(raw["validation_status"]),
            "status_reason": str(raw["status_reason"]),
            "accepted_commit_id": raw["accepted_commit_id"],
            "artifact_id": str(raw["artifact_id"]),
            "artifact_stage": str(raw["artifact_stage"]),
            "branch_id": str(raw["branch_id"]),
            "chapter_no": raw["chapter_no"],
            "scene_index": raw["scene_index"],
            "artifact_revision": int(raw["artifact_revision"]),
            "prepared_canon_revision": int(
                raw["prepared_canon_revision"]
            ),
            "missing": False,
        }

    def barrier_status(
        self,
        *,
        branch_id: str,
        sequence_no: int | None,
        include_prior: bool = False,
    ) -> dict[str, Any]:
        """Return the story-continuity barrier for one branch/sequence.

        ``include_prior=True`` checks all numbered jobs through
        ``sequence_no``.  The default exact match is useful when the caller
        already resolved the previous sequence number.
        """

        branch = _require_string(branch_id, "branch_id")
        sequence = _require_int(
            sequence_no,
            "sequence_no",
            allow_none=True,
        )
        if not isinstance(include_prior, bool):
            raise ExtractionJobError(
                "EXTRACTION_JOB_INVALID_ARGUMENT",
                "include_prior must be boolean",
                details={"field": "include_prior"},
            )
        if include_prior and sequence is None:
            raise ExtractionJobError(
                "EXTRACTION_JOB_INVALID_ARGUMENT",
                "include_prior requires a numbered sequence",
                details={"field": "sequence_no"},
            )
        if sequence is None:
            sequence_clause = "sequence_no IS NULL"
            sequence_params: list[Any] = []
        elif include_prior:
            # Legacy unnumbered story jobs are included fail-closed so a
            # numbered caller cannot silently skip them.
            sequence_clause = "(sequence_no IS NULL OR sequence_no<=?)"
            sequence_params = [sequence]
        else:
            sequence_clause = "sequence_no=?"
            sequence_params = [sequence]

        with self.store.read_connection() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                f"""
                SELECT *
                FROM extraction_jobs
                WHERE branch_id=? AND {sequence_clause}
                ORDER BY
                    CASE WHEN sequence_no IS NULL THEN -1 ELSE sequence_no END,
                    created_at,
                    job_id
                """,
                [branch, *sequence_params],
            ).fetchall()
            proposal_ids = sorted(
                {
                    str(row["result_proposal_id"])
                    for row in rows
                    if row["result_proposal_id"] is not None
                }
            )
            proposals: dict[str, sqlite3.Row] = {}
            if proposal_ids:
                placeholders = ",".join("?" for _ in proposal_ids)
                proposal_rows = connection.execute(
                    f"""
                    SELECT proposal_id, artifact_id, artifact_stage, branch_id,
                           chapter_no, scene_index, artifact_revision,
                           prepared_canon_revision, canon_status,
                           validation_status, status_reason,
                           accepted_commit_id, payload_json
                    FROM proposals
                    WHERE proposal_id IN ({placeholders})
                    """,
                    proposal_ids,
                ).fetchall()
                proposals = {
                    str(row["proposal_id"]): row for row in proposal_rows
                }
            commit_ids = sorted(
                {
                    str(row["accepted_commit_id"])
                    for row in proposals.values()
                    if row["accepted_commit_id"] is not None
                }
            )
            commits: dict[str, sqlite3.Row] = {}
            if commit_ids:
                placeholders = ",".join("?" for _ in commit_ids)
                commit_rows = connection.execute(
                    f"""
                    SELECT *
                    FROM canon_commits
                    WHERE commit_id IN ({placeholders})
                    """,
                    commit_ids,
                ).fetchall()
                commits = {
                    str(row["commit_id"]): row for row in commit_rows
                }
            job_ids = [str(row["job_id"]) for row in rows]
            resolution_rows: list[sqlite3.Row] = []
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                resolution_rows = connection.execute(
                    f"""
                    SELECT *
                    FROM extraction_barrier_resolutions
                    WHERE job_id IN ({placeholders})
                    """,
                    job_ids,
                ).fetchall()
            proposal_checks: dict[str, dict[str, Any]] = {}
            for job_row in rows:
                proposal_id = job_row["result_proposal_id"]
                if proposal_id is None:
                    continue
                job_id = str(job_row["job_id"])
                proposal = proposals.get(str(proposal_id))
                if proposal is None:
                    proposal_checks[job_id] = {
                        "error_code": "EXTRACTION_PROPOSAL_NOT_FOUND",
                        "error": (
                            "succeeded extraction job references a missing "
                            "proposal"
                        ),
                        "shadow": False,
                    }
                    continue
                try:
                    decoded_job, payload = self._validate_proposal_identity(
                        connection,
                        job_row,
                        proposal,
                    )
                    disposition = str(proposal["canon_status"])
                    shadow = False
                    if disposition == "accepted":
                        commit_id = str(
                            proposal["accepted_commit_id"] or ""
                        )
                        self._validate_accepted_barrier_commit(
                            proposal,
                            commits.get(commit_id),
                        )
                    elif disposition == "proposed":
                        shadow = self._validate_result_proposal_status(
                            decoded_job,
                            proposal,
                            payload,
                        )
                    elif (
                        disposition == "rejected"
                        and self._shadow_configuration(decoded_job)
                        is not None
                    ):
                        shadow = self._validate_result_proposal_status(
                            decoded_job,
                            proposal,
                            payload,
                        )
                    elif disposition not in {"rejected", "retracted"}:
                        raise ExtractionJobConflict(
                            "EXTRACTION_PROPOSAL_STATUS_CONFLICT",
                            "proposal has an unknown barrier disposition",
                            details={
                                "proposal_id": str(proposal_id),
                                "canon_status": disposition,
                            },
                        )
                    proposal_checks[job_id] = {
                        "error_code": "",
                        "error": "",
                        "shadow": shadow,
                    }
                except ExtractionJobError as exc:
                    proposal_checks[job_id] = {
                        "error_code": exc.code,
                        "error": _safe_error(exc),
                        "shadow": False,
                    }

        decoded = [self._decode_row(row) for row in rows]
        resolutions = {
            str(row["job_id"]): self._decode_resolution(row)
            for row in resolution_rows
        }
        resolved: list[dict[str, Any]] = []
        selected_job: dict[str, Any] | None = None
        selected_proposal: dict[str, Any] | None = None
        code = "clear"
        blocking = False
        accepted_job: dict[str, Any] | None = None
        accepted_proposal: dict[str, Any] | None = None

        for job in decoded:
            resolution = resolutions.get(str(job["job_id"]))
            if resolution is not None:
                resolved.append(resolution)
                continue
            status = str(job["status"])
            if status in {"queued", "running", "failed", "cancelled"}:
                selected_job = job
                code = status
                blocking = True
                break
            if status != "succeeded":
                # A corrupted future status is never treated as clear.
                selected_job = job
                code = "failed"
                blocking = True
                break
            result_kind = str(job.get("result_kind") or "")
            proposal_id = job.get("result_proposal_id")
            if (
                result_kind not in RESULT_KINDS
                or (result_kind == "proposal") != (proposal_id is not None)
            ):
                selected_job = job
                code = "failed"
                blocking = True
                break
            if result_kind == "no_delta":
                continue
            proposal = self._proposal_summary(
                proposals.get(str(proposal_id)),
                str(proposal_id),
            )
            check = proposal_checks.get(
                str(job["job_id"]),
                {
                    "error_code": "EXTRACTION_PROPOSAL_NOT_FOUND",
                    "error": "proposal validation result is missing",
                    "shadow": False,
                },
            )
            if check["error_code"]:
                proposal["barrier_error_code"] = str(
                    check["error_code"]
                )
                proposal["barrier_error"] = str(check["error"])
                selected_job = job
                selected_proposal = proposal
                code = "failed"
                blocking = True
                break
            if bool(check["shadow"]):
                continue
            disposition = str(proposal["canon_status"])
            if disposition == "accepted":
                accepted_job = job
                accepted_proposal = proposal
                continue
            if disposition in {"rejected", "retracted"}:
                selected_job = job
                selected_proposal = proposal
                code = disposition
                blocking = True
                break
            if disposition == "proposed":
                selected_job = job
                selected_proposal = proposal
                code = "pending_review"
                blocking = True
                break
            selected_job = job
            selected_proposal = proposal
            code = "failed"
            blocking = True
            break

        if not blocking and accepted_job is not None:
            code = "accepted"
            selected_job = accepted_job
            selected_proposal = accepted_proposal

        if code not in BARRIER_CODES:
            raise AssertionError(f"invalid barrier code: {code}")
        return {
            "code": code,
            "blocking": blocking,
            "branch_id": branch,
            "sequence_no": sequence,
            "include_prior": include_prior,
            "job": selected_job,
            "proposal": selected_proposal,
            "job_count": len(decoded),
            "resolved_job_count": len(resolved),
            "resolutions": resolved,
        }

    barrier = barrier_status


# Compatibility-oriented names for integration layers that prefer a service or
# store noun.  All three names address the same durable implementation.
ExtractionJobStore = ExtractionJobQueue
ExtractionJobService = ExtractionJobQueue


__all__ = [
    "BARRIER_CODES",
    "BARRIER_RESOLUTION_ACTIONS",
    "JOB_STATUSES",
    "MAX_ASSISTANT_PAYLOAD_BYTES",
    "RESULT_KINDS",
    "TERMINAL_JOB_STATUSES",
    "ExtractionJobConflict",
    "ExtractionJobError",
    "ExtractionJobNotFound",
    "ExtractionJobQueue",
    "ExtractionJobService",
    "ExtractionJobStore",
    "ExtractionLeaseLost",
    "ExtractionWorkResult",
]
