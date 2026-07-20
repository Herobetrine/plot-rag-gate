"""Proposal-only SiliconFlow ambiguity review for story initialization.

The local deterministic inventory and extractor always run first.  This
module is only an optional ambiguity reviewer and never owns authority,
canonical state, identifiers, files, or database writes.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from .canonical import canonical_hash, sha256_text
from .errors import PlotInitError
from .remote_cache import (
    REMOTE_CACHE_IDENTITY_PROTOCOL,
    RemoteResponseCache,
    normalize_remote_base_url,
    sanitize_remote_cache_value,
)


REMOTE_REVIEW_PROTOCOL = "plot-rag-init-remote-review/v1"
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
DEFAULT_TIMEOUT_SECONDS = 30.0
REMOTE_TEMPERATURE = 0.0
REMOTE_TOP_P = 1.0
REMOTE_MAX_TOKENS = 2400
REMOTE_RESPONSE_FORMAT = {"type": "json_object"}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_CHARS = 96_000
LOW_CONFIDENCE_THRESHOLD = 0.70

_TRUE_VALUES = {"1", "true", "yes", "on"}
_BUILTIN_TRUSTED_HOSTS = {"api.siliconflow.cn"}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_SOURCE_ROLES = {"canon", "setting", "outline", "draft", "note", "reference"}
_PREDICATE_RE = re.compile(
    r"^(?:"
    r"actor(?:\.[a-z][a-z0-9_]*)+|"
    r"ability(?:\.[a-z][a-z0-9_]*)+|"
    r"power(?:\.[a-z][a-z0-9_]*)+|"
    r"progression(?:\.[a-z][a-z0-9_]*)+|"
    r"rank(?:\.[a-z][a-z0-9_]*)+|"
    r"resource(?:\.[a-z][a-z0-9_]*)+|"
    r"status(?:\.[a-z][a-z0-9_]*)+|"
    r"binding(?:\.[a-z][a-z0-9_]*)+|"
    r"qualification(?:\.[a-z][a-z0-9_]*)+|"
    r"conversion(?:\.[a-z][a-z0-9_]*)+|"
    r"observation(?:\.[a-z][a-z0-9_]*)+|"
    r"counter(?:\.[a-z][a-z0-9_]*)+|"
    r"bridge(?:\.[a-z][a-z0-9_]*)+|"
    r"entity\.alias|"
    r"faction(?:\.[a-z][a-z0-9_]*)+|"
    r"genre(?:\.[a-z][a-z0-9_]*)+|"
    r"inventory(?:\.[a-z][a-z0-9_]*)+|"
    r"relation(?:\.[a-z][a-z0-9_]*)*|"
    r"serialization(?:\.[a-z][a-z0-9_]*)+|"
    r"story(?:\.[a-z][a-z0-9_]*)+|"
    r"timeline(?:\.[a-z][a-z0-9_]*)+|"
    r"world(?:\.[a-z][a-z0-9_]*)+|"
    r"open_loop"
    r")$"
)

_CLASSIFICATION_SCHEMA = {
    "type": "object",
    "required": ["source_role", "confidence", "exact_evidence"],
    "additionalProperties": False,
    "properties": {
        "source_role": {"enum": sorted(_SOURCE_ROLES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "exact_evidence": {"type": "string", "minLength": 1},
    },
}

_CLAIM_SCHEMA = {
    "type": "object",
    "required": ["claims"],
    "additionalProperties": False,
    "properties": {
        "claims": {
            "type": "array",
            "minItems": 1,
            "maxItems": 24,
            "items": {
                "type": "object",
                "required": [
                    "subject",
                    "predicate",
                    "object_or_value",
                    "exact_evidence",
                    "confidence",
                ],
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {
                        "type": "string",
                        "pattern": _PREDICATE_RE.pattern,
                    },
                    "object_or_value": {},
                    "exact_evidence": {"type": "string"},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
                "additionalProperties": False,
            },
        }
    },
}


class RemoteModelError(RuntimeError):
    """Stable, secret-free failure surfaced by the optional reviewer."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "REMOTE_REVIEW_FAILED")
        super().__init__(self.code)


@dataclass(frozen=True)
class RemoteModelConfig:
    enabled: bool
    provider: str
    base_url: str
    model: str
    api_key: str
    timeout_seconds: float
    host: str
    ready: bool
    error_code: str | None = None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
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
            raise RemoteModelError("REMOTE_REDIRECT_BLOCKED")


def _trusted_hosts() -> set[str]:
    configured = {
        item.strip().casefold().rstrip(".")
        for item in re.split(
            r"[,;\s]+",
            os.environ.get("PLOT_RAG_TRUSTED_HOSTS", ""),
        )
        if item.strip()
    }
    return _BUILTIN_TRUSTED_HOSTS | configured


def _is_loopback(host: str) -> bool:
    normalized = host.casefold().rstrip(".")
    return normalized in _LOOPBACK_HOSTS or normalized.endswith(".localhost")


def load_remote_model_config() -> RemoteModelConfig:
    enabled = (
        os.environ.get("PLOT_RAG_INIT_REMOTE_ENABLED", "").strip().casefold()
        in _TRUE_VALUES
    )
    raw_base_url = (
        os.environ.get("PLOT_RAG_LLM_BASE_URL", "").strip()
        or DEFAULT_BASE_URL
    )
    model = (
        os.environ.get("PLOT_RAG_LLM_MODEL", "").strip() or DEFAULT_MODEL
    )
    dedicated_api_key = os.environ.get("PLOT_RAG_LLM_API_KEY", "").strip()
    shared_siliconflow_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    api_key = dedicated_api_key or shared_siliconflow_key

    timeout_raw = os.environ.get(
        "PLOT_RAG_LLM_TIMEOUT_SECONDS",
        str(DEFAULT_TIMEOUT_SECONDS),
    ).strip()
    try:
        timeout_seconds = float(timeout_raw)
    except ValueError:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        timeout_error = "REMOTE_CONFIG_TIMEOUT_INVALID"
    else:
        timeout_error = None
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds < 0.05
            or timeout_seconds > 300.0
        ):
            timeout_seconds = DEFAULT_TIMEOUT_SECONDS
            timeout_error = "REMOTE_CONFIG_TIMEOUT_INVALID"

    error_code: str | None = timeout_error
    host = ""
    base_url = ""
    try:
        base_url = normalize_remote_base_url(raw_base_url)
        parsed = urllib.parse.urlsplit(base_url)
        host = (parsed.hostname or "").casefold().rstrip(".")
    except (PlotInitError, ValueError):
        parsed = urllib.parse.SplitResult("", "", "", "", "")
        error_code = error_code or "REMOTE_CONFIG_URL_INVALID"
    provider = (
        "siliconflow"
        if host == "api.siliconflow.cn"
        else "openai-compatible"
        if host
        else "unknown"
    )

    if not enabled:
        error_code = "REMOTE_DISABLED"
    elif error_code is None and (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        error_code = "REMOTE_CONFIG_URL_INVALID"
    elif error_code is None and host not in _trusted_hosts():
        error_code = "REMOTE_HOST_UNTRUSTED"
    elif (
        error_code is None
        and not dedicated_api_key
        and bool(shared_siliconflow_key)
        and host != "api.siliconflow.cn"
    ):
        # The shared provider credential has a fixed egress boundary.  A
        # custom allow-listed host must use the dedicated LLM key instead of
        # inheriting SILICONFLOW_API_KEY.
        error_code = "REMOTE_CREDENTIAL_HOST_MISMATCH"
    elif (
        error_code is None
        and parsed.scheme != "https"
        and not _is_loopback(host)
    ):
        error_code = "REMOTE_INSECURE_TRANSPORT"
    elif error_code is None and not api_key:
        error_code = "REMOTE_API_KEY_MISSING"
    elif error_code is None and not model:
        error_code = "REMOTE_MODEL_MISSING"

    return RemoteModelConfig(
        enabled=enabled,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        host=host,
        ready=error_code is None,
        error_code=error_code,
    )


def _chat_url(config: RemoteModelConfig) -> str:
    parsed = urllib.parse.urlsplit(config.base_url)
    path = parsed.path.rstrip("/")
    if path.casefold().endswith("/chat/completions"):
        target_path = path
    else:
        target_path = f"{path}/chat/completions"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, target_path, "", "")
    )


def _effective_generation_parameters() -> dict[str, Any]:
    """Return the exact non-message generation fields sent to the provider."""

    return {
        "temperature": REMOTE_TEMPERATURE,
        "top_p": REMOTE_TOP_P,
        "max_tokens": REMOTE_MAX_TOKENS,
        "response_format": dict(REMOTE_RESPONSE_FORMAT),
    }


def _remote_json(
    config: RemoteModelConfig,
    *,
    system_prompt: str,
    user_payload: Mapping[str, Any],
) -> dict[str, Any]:
    if not config.ready:
        raise RemoteModelError(config.error_code or "REMOTE_NOT_READY")

    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    dict(user_payload),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ],
        **_effective_generation_parameters(),
    }
    request = urllib.request.Request(
        _chat_url(config),
        data=json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "close",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=config.timeout_seconds) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except RemoteModelError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            if 300 <= int(exc.code) < 400:
                raise RemoteModelError("REMOTE_REDIRECT_BLOCKED") from exc
            if int(exc.code) == 429:
                raise RemoteModelError("REMOTE_HTTP_429") from exc
            raise RemoteModelError(f"REMOTE_HTTP_{int(exc.code)}") from exc
        finally:
            exc.close()
    except (socket.timeout, TimeoutError):
        raise RemoteModelError("REMOTE_TIMEOUT")
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(getattr(exc, "reason", None), socket.timeout):
            raise RemoteModelError("REMOTE_TIMEOUT") from exc
        raise RemoteModelError("REMOTE_NETWORK_ERROR") from exc

    if len(raw) > MAX_RESPONSE_BYTES:
        raise RemoteModelError("REMOTE_RESPONSE_TOO_LARGE")
    if not raw.strip():
        raise RemoteModelError("REMOTE_RESPONSE_EMPTY")
    try:
        envelope = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteModelError("REMOTE_RESPONSE_INVALID_JSON") from exc
    if not isinstance(envelope, dict):
        raise RemoteModelError("REMOTE_RESPONSE_SCHEMA_INVALID")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RemoteModelError("REMOTE_RESPONSE_SCHEMA_INVALID")
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RemoteModelError("REMOTE_CONTENT_EMPTY")
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RemoteModelError("REMOTE_CONTENT_INVALID_JSON") from exc
    if not isinstance(value, dict):
        raise RemoteModelError("REMOTE_CONTENT_SCHEMA_INVALID")
    sanitized = sanitize_remote_cache_value(value)
    if not isinstance(sanitized, dict):
        raise RemoteModelError("REMOTE_CONTENT_SCHEMA_INVALID")
    return sanitized


def _bounded_source(text: str) -> str:
    value = str(text or "")
    if len(value) <= MAX_SOURCE_CHARS:
        return value
    head = MAX_SOURCE_CHARS * 3 // 4
    tail = MAX_SOURCE_CHARS - head
    return value[:head] + "\n[...SOURCE_TRUNCATED...]\n" + value[-tail:]


def _continuous_evidence(source_text: str, evidence: Any) -> str:
    if not isinstance(evidence, str):
        raise RemoteModelError("REMOTE_EVIDENCE_INVALID")
    exact = evidence.strip()
    if not exact or exact not in source_text:
        raise RemoteModelError("REMOTE_EVIDENCE_INVALID")
    return exact


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RemoteModelError("REMOTE_CONTENT_SCHEMA_INVALID")
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > 1:
        raise RemoteModelError("REMOTE_CONTENT_SCHEMA_INVALID")
    return number


def _text_leaves(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        result: list[str] = []
        for child in value:
            result.extend(_text_leaves(child))
        return result
    if isinstance(value, dict):
        result = []
        for child in value.values():
            result.extend(_text_leaves(child))
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return []
    raise RemoteModelError("REMOTE_CONTENT_SCHEMA_INVALID")


def _line_span(source_text: str, evidence: str) -> tuple[int, int]:
    start_offset = source_text.find(evidence)
    if start_offset < 0:
        raise RemoteModelError("REMOTE_EVIDENCE_INVALID")
    line_start = source_text.count("\n", 0, start_offset) + 1
    line_end = line_start + evidence.count("\n")
    return line_start, line_end


def _validate_classification(
    value: Mapping[str, Any],
    *,
    source_text: str,
) -> dict[str, Any]:
    allowed = {
        "source_role",
        "confidence",
        "exact_evidence",
        "authority_tier",
        "ingest_policy",
        "artifact_stage",
        "scope_policy",
        "canon_status",
        "scope",
        "field_status",
    }
    if not {"source_role", "confidence", "exact_evidence"} <= set(value):
        raise RemoteModelError("REMOTE_CONTENT_SCHEMA_INVALID")
    if not set(value) <= allowed:
        raise RemoteModelError("REMOTE_CONTENT_SCHEMA_INVALID")
    role = value.get("source_role")
    if not isinstance(role, str) or role not in _SOURCE_ROLES:
        raise RemoteModelError("REMOTE_CONTENT_SCHEMA_INVALID")
    evidence = _continuous_evidence(source_text, value.get("exact_evidence"))
    return {
        "source_role": role,
        "confidence": _number(value.get("confidence")),
        "exact_evidence": evidence,
    }


def _validate_claims(
    value: Mapping[str, Any],
    *,
    source_text: str,
) -> dict[str, Any]:
    if set(value) != {"claims"}:
        raise RemoteModelError("REMOTE_CONTENT_SCHEMA_INVALID")
    raw_claims = value.get("claims")
    if not isinstance(raw_claims, list) or not 1 <= len(raw_claims) <= 24:
        raise RemoteModelError("REMOTE_CLAIMS_EMPTY")
    claims: list[dict[str, Any]] = []
    required = {
        "subject",
        "predicate",
        "object_or_value",
        "exact_evidence",
        "confidence",
    }
    optional = {
        "authority_tier",
        "ingest_policy",
        "canon_status",
        "scope",
        "field_status",
        "origin",
        "line_start",
        "line_end",
    }
    for raw_claim in raw_claims:
        if (
            not isinstance(raw_claim, dict)
            or not required <= set(raw_claim)
            or not set(raw_claim) <= required | optional
        ):
            raise RemoteModelError("REMOTE_CONTENT_SCHEMA_INVALID")
        subject = raw_claim.get("subject")
        predicate = raw_claim.get("predicate")
        if not isinstance(subject, str) or not subject.strip():
            raise RemoteModelError("REMOTE_CONTENT_SCHEMA_INVALID")
        if not isinstance(predicate, str) or not _PREDICATE_RE.fullmatch(predicate):
            raise RemoteModelError("REMOTE_CONTENT_SCHEMA_INVALID")
        value_payload = raw_claim.get("object_or_value")
        evidence = _continuous_evidence(
            source_text,
            raw_claim.get("exact_evidence"),
        )
        leaves = [subject.strip(), *_text_leaves(value_payload)]
        if any(leaf not in evidence for leaf in leaves):
            raise RemoteModelError("REMOTE_EVIDENCE_INVALID")
        line_start, line_end = _line_span(source_text, evidence)
        claims.append(
            {
                "subject": subject.strip(),
                "predicate": predicate,
                "object_or_value": value_payload,
                "exact_evidence": evidence,
                "line_start": line_start,
                "line_end": line_end,
                "confidence": _number(raw_claim.get("confidence")),
            }
        )
    return {"claims": claims}


def _diagnostics(
    *,
    config: RemoteModelConfig,
    status: str,
    system_prompt: str,
    generation_parameters: Mapping[str, Any],
    cache_identity: Mapping[str, Any] | None = None,
    error_code: str | None = None,
    cache_hit: bool = False,
    accepted_count: int = 0,
    rejected_count: int = 0,
    response_hash: str | None = None,
) -> dict[str, Any]:
    generation_payload = dict(generation_parameters)
    return {
        "protocol": REMOTE_REVIEW_PROTOCOL,
        "status": status,
        "error_code": error_code,
        "provider": config.provider,
        "base_url": config.base_url,
        "model": config.model,
        "cache_identity_protocol": REMOTE_CACHE_IDENTITY_PROTOCOL,
        "cache_key": (
            str(cache_identity.get("cache_key"))
            if cache_identity and cache_identity.get("cache_key")
            else None
        ),
        "system_prompt_hash": sha256_text(system_prompt),
        "generation_parameters": generation_payload,
        "generation_parameters_hash": canonical_hash(generation_payload),
        "cache_hit": bool(cache_hit),
        "accepted_count": int(accepted_count),
        "rejected_count": int(rejected_count),
        "response_hash": response_hash,
    }


def _resolve(
    *,
    task: str,
    path: str,
    source_text: str,
    source_hash: str,
    context: Mapping[str, Any],
    schema: Mapping[str, Any],
    remote_cache: RemoteResponseCache | None,
) -> dict[str, Any]:
    config = load_remote_model_config()
    if task == "classification":
        system_prompt = (
            "你是网文资料歧义复核器。只输出一个严格 JSON 对象。"
            "你只能提出来源角色候选，不能授予正典、include、current 或 timeless 权限。"
            "只输出 output_schema 要求的 source_role、confidence、exact_evidence 三个字段，"
            "不要输出 authority_tier、ingest_policy、artifact_stage、scope、canon_status "
            "或 field_status。exact_evidence 必须是 source_text 中连续逐字片段。"
        )
        validator = _validate_classification
    else:
        system_prompt = (
            "你是网文资料事实候选抽取器。只输出一个严格 JSON 对象。"
            "每条 claim 只输出 subject、predicate、object_or_value、exact_evidence、"
            "confidence 五个字段，不要输出任何权限、来源、scope 或正典字段。"
            "predicate 必须是英文稳定标识：actor.*、ability.*、entity.alias、"
            "power.*、progression.*、rank.*、resource.*、status.*、binding.*、"
            "qualification.*、conversion.*、observation.*、counter.*、"
            "bridge.*、faction.*、genre.*、"
            "inventory.*、relation.*、serialization.*、"
            "story.*、timeline.*、world.* 或 open_loop；世界规则使用 world.rule，"
            "人物目标使用 actor.goal；能力持有使用 ability.owns，能力来源、代价、"
            "限制、反制和冷却分别使用 ability.source/cost/limit/counter/cooldown。"
            "体系、成长轨、阶段、资源、状态、资格和换算规则分别优先使用"
            "power.system、progression.track、rank.node/rank.edge、"
            "resource.definition/state、status.definition/state、"
            "qualification.definition/state、conversion.rule；"
            "已观察但未确认的能力使用 observation.capability。"
            "subject、object_or_value 的每个文本叶子以及"
            " exact_evidence 必须逐字出现在同一证据片段中。"
        )
        validator = _validate_claims
    generation_parameters = _effective_generation_parameters()
    if not config.ready or remote_cache is None:
        code = config.error_code or "REMOTE_CACHE_UNAVAILABLE"
        return {
            "proposal": None,
            "diagnostics": _diagnostics(
                config=config,
                status="skipped" if code == "REMOTE_DISABLED" else "failed",
                error_code=code,
                system_prompt=system_prompt,
                generation_parameters=generation_parameters,
            ),
        }

    bounded = _bounded_source(source_text)
    user_payload = {
        "protocol": REMOTE_REVIEW_PROTOCOL,
        "task": task,
        "source_path": path,
        "source_text": bounded,
        "local_context": dict(context),
        "output_schema": dict(schema),
    }

    def loader() -> dict[str, Any]:
        raw = _remote_json(
            config,
            system_prompt=system_prompt,
            user_payload=user_payload,
        )
        return validator(raw, source_text=source_text)

    try:
        resolved = remote_cache.resolve(
            provider=config.provider,
            base_url=config.base_url,
            model=config.model,
            prompt={
                "protocol": REMOTE_REVIEW_PROTOCOL,
                "task": task,
                "path": path,
                "source_excerpt": bounded,
                "context": dict(context),
            },
            system_prompt=system_prompt,
            schema=schema,
            source_hash=source_hash,
            generation_parameters=generation_parameters,
            loader=loader,
        )
        response = resolved.get("response")
        if not isinstance(response, dict):
            raise RemoteModelError("REMOTE_CACHE_RESPONSE_INVALID")
        proposal = validator(response, source_text=source_text)
        count = (
            len(proposal.get("claims") or [])
            if task == "claims"
            else 1
        )
        response_hash = canonical_hash(proposal)
        return {
            "proposal": proposal,
            "diagnostics": _diagnostics(
                config=config,
                status="accepted",
                system_prompt=system_prompt,
                generation_parameters=generation_parameters,
                cache_identity=resolved,
                cache_hit=bool(resolved.get("cache_hit")),
                accepted_count=count,
                response_hash=response_hash,
            ),
            "response_hash": response_hash,
        }
    except RemoteModelError as exc:
        return {
            "proposal": None,
            "diagnostics": _diagnostics(
                config=config,
                status="failed",
                error_code=exc.code,
                system_prompt=system_prompt,
                generation_parameters=generation_parameters,
                rejected_count=1,
            ),
        }
    except Exception:
        # The remote reviewer is optional.  Unexpected cache/provider adapter
        # failures degrade to a stable code without exposing exception text.
        return {
            "proposal": None,
            "diagnostics": _diagnostics(
                config=config,
                status="failed",
                error_code="REMOTE_CACHE_ERROR",
                system_prompt=system_prompt,
                generation_parameters=generation_parameters,
                rejected_count=1,
            ),
        }


def resolve_classification_review(
    *,
    path: str,
    source_text: str,
    source_hash: str,
    local_classification: Mapping[str, Any],
    remote_cache: RemoteResponseCache | None,
) -> dict[str, Any]:
    return _resolve(
        task="classification",
        path=path,
        source_text=source_text,
        source_hash=source_hash,
        context={
            "source_role": local_classification.get("source_role"),
            "classification_confidence": local_classification.get(
                "classification_confidence"
            ),
            "classification_basis": local_classification.get(
                "classification_basis"
            ),
        },
        schema=_CLASSIFICATION_SCHEMA,
        remote_cache=remote_cache,
    )


def resolve_claim_review(
    *,
    path: str,
    source_text: str,
    source_hash: str,
    local_claim_count: int,
    classification_confidence: float,
    remote_cache: RemoteResponseCache | None,
) -> dict[str, Any]:
    return _resolve(
        task="claims",
        path=path,
        source_text=source_text,
        source_hash=source_hash,
        context={
            "local_claim_count": int(local_claim_count),
            "classification_confidence": float(classification_confidence),
        },
        schema=_CLAIM_SCHEMA,
        remote_cache=remote_cache,
    )
