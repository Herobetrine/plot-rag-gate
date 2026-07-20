"""Pure proposal-only initialization state machine and bundle normalization."""

from __future__ import annotations

import copy
import difflib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .canonical import (
    canonical_hash,
    canonical_json,
    json_pointer,
    path_is_within,
    sha256_bytes,
    stable_id,
    utc_now,
)
from .constants import (
    INTERACTION_PROFILES,
    KNOWLEDGE_PLANES,
    MODES,
    MVW_FIELDS,
    POWER_ARTIFACTS,
    PRESSURE_TESTS,
    PROTOCOL_AUTO,
    PROTOCOL_V1,
    PROTOCOL_V2,
    PROTOCOL_VERSION,
    QUESTION_DEPENDENCIES,
    RESOLUTION_LEVELS,
    ROUTING_MODES,
    STANDARD_ARTIFACTS,
    TARGET_PROFILES,
    WORLD_MODULES,
    WORLD_OBJECT_TYPES,
)
from .errors import PlotInitError
from .inventory import entity_type_for_claim, extract_claims, inventory_sources
from .advantages import (
    ADVANTAGE_DOSSIER_KEYS,
    advantage_package_from_artifact_manifest,
    advantage_package_has_typed_content,
    advantage_sidecar_reference,
    build_advantage_package,
    build_advantage_sidecar_artifact,
)
from .items import (
    ITEM_DOSSIER_KEYS,
    build_item_package,
    build_item_sidecar_artifact,
    item_package_from_artifact_manifest,
    item_package_has_typed_content,
    item_sidecar_reference,
)
from .normalized import (
    normalization_diff,
    normalized_hash,
    parse_normalized_export,
    recompute_bundle_hash,
)
try:  # ``scripts`` package import used by unittest/distribution tooling.
    from ..power_system import (
        build_power_package,
        negotiate_initialization_schema,
        power_sufficiency,
    )
except (ImportError, ValueError):  # Top-level ``plot_init`` used by CLI/MCP.
    from power_system import (
        build_power_package,
        negotiate_initialization_schema,
        power_sufficiency,
    )

if TYPE_CHECKING:
    from .remote_cache import RemoteResponseCache


DOMAIN_KEYS = (
    "genre_contract",
    "world_model",
    "actor_system",
    "story_engine",
    "serialization_contract",
    "entities",
    "relations",
    "timeline",
    "open_loops",
    "power_profile",
    "power_system",
    "power_systems",
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
    "actor_power_bootstrap",
)

ITEM_DOMAIN_KEYS = tuple(ITEM_DOSSIER_KEYS)
ADVANTAGE_DOMAIN_KEYS = tuple(ADVANTAGE_DOSSIER_KEYS)

POWER_DOMAIN_KEYS = (
    "power_systems",
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
    "actor_power_bootstrap",
)

TOP_LEVEL_ALIASES = {
    "题材合同": "genre_contract",
    "题材": "genre_contract",
    "世界模型": "world_model",
    "世界": "world_model",
    "人物系统": "actor_system",
    "角色": "actor_system",
    "剧情发动机": "story_engine",
    "剧情": "story_engine",
    "连载合同": "serialization_contract",
    "连载兑现合同": "serialization_contract",
    "实体": "entities",
    "关系": "relations",
    "时间线": "timeline",
    "未决剧情债务": "open_loops",
    "力量类型": "power_profile",
    "力量体系": "power_system",
    "力量系统": "power_system",
    "成长轨": "progression_tracks",
    "境界节点": "rank_nodes",
    "晋升边": "rank_edges",
    "能力定义": "ability_definitions",
    "资源定义": "resource_definitions",
    "状态定义": "status_definitions",
    "资格定义": "qualification_definitions",
    "克制规则": "counter_rules",
    "桥接规则": "bridge_rules",
    "转换规则": "conversion_rules",
    "角色力量初态": "actor_power_bootstrap",
    "物品": "items",
    "物品定义": "item_definitions",
    "物品实例": "item_instances",
    "物品堆": "item_stacks",
    "物品功能": "item_functions",
    "物品功能绑定": "item_function_bindings",
    "物品保管初态": "item_custody_bootstrap",
    "物品运行初态": "item_runtime_bootstrap",
    "物品功能运行初态": "item_function_runtime_bootstrap",
    "物品观察": "item_observations",
    "金手指": "advantages",
    "金手指定义": "advantage_definitions",
    "金手指锚点": "advantage_anchors",
    "金手指模块": "advantage_modules",
    "金手指运行槽": "advantage_runtime_slots",
    "金手指运行初态": "advantage_runtime",
    "金手指账本初态": "advantage_ledger",
    "金手指知识": "advantage_knowledge",
    "金手指契约": "advantage_contracts",
    "金手指叙事契约": "advantage_narrative_contracts",
}


def _deep_merge(base: Any, overlay: Any) -> Any:
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = copy.deepcopy(base)
        for key, value in overlay.items():
            if key in merged:
                merged[key] = _deep_merge(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged
    if isinstance(base, list) and isinstance(overlay, list):
        result = copy.deepcopy(base)
        seen = {canonical_json(item) for item in result}
        for item in overlay:
            encoded = canonical_json(item)
            if encoded not in seen:
                result.append(copy.deepcopy(item))
                seen.add(encoded)
        return result
    return copy.deepcopy(overlay)


def _set_pointer(target: dict[str, Any], pointer: str, value: Any) -> None:
    if not pointer.startswith("/"):
        raise PlotInitError("INVALID_PATCH_PATH", f"JSON pointer must start with '/': {pointer}")
    parts = [
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer.lstrip("/").split("/")
        if token != ""
    ]
    if not parts:
        raise PlotInitError("INVALID_PATCH_PATH", "root replacement is not supported")
    cursor: dict[str, Any] = target
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = copy.deepcopy(value)


def _walk_leaves(value: Any, parts: tuple[str, ...] = ()) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        if not value:
            yield json_pointer(parts), {}
            return
        for key, child in value.items():
            yield from _walk_leaves(child, (*parts, str(key)))
        return
    if isinstance(value, list):
        if not value:
            yield json_pointer(parts), []
            return
        for index, child in enumerate(value):
            yield from _walk_leaves(child, (*parts, str(index)))
        return
    yield json_pointer(parts), value


def _field_state(
    *,
    field_status: str,
    origin: str,
    decision_status: str = "session_locked",
    source_refs: list[str] | None = None,
    confidence: float = 1.0,
    scope: str | None = None,
    knowledge_plane: str = "objective",
    branch_id: str = "main",
    depends_on: list[str] | None = None,
    invalidates: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "field_status": field_status,
        "origin": origin,
        "decision_status": decision_status,
        "canon_status": "proposed",
        "source_refs": list(source_refs or []),
        "confidence": float(confidence),
        "scope": scope,
        "knowledge_plane": knowledge_plane,
        "branch_id": branch_id,
        "depends_on": list(depends_on or []),
        "invalidates": list(invalidates or []),
    }


def _states_for_payload(
    payload: dict[str, Any],
    *,
    field_status: str,
    origin: str,
    decision_status: str = "session_locked",
    source_refs: list[str] | None = None,
    confidence: float = 1.0,
    scope: str | None = None,
    knowledge_plane: str = "objective",
    branch_id: str = "main",
    depends_on: list[str] | None = None,
    invalidates: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for pointer, _ in _walk_leaves(payload):
        result[pointer] = _field_state(
            field_status=field_status,
            origin=origin,
            decision_status=decision_status,
            source_refs=source_refs,
            confidence=confidence,
            scope=scope,
            knowledge_plane=knowledge_plane,
            branch_id=branch_id,
            depends_on=depends_on,
            invalidates=invalidates,
        )
    return result


def _normalize_seed(seed: Any) -> tuple[Any, dict[str, Any], dict[str, dict[str, Any]]]:
    if seed is None:
        return None, {}, {}
    raw = copy.deepcopy(seed)
    if isinstance(seed, dict):
        candidate = seed.get("initialization_bundle", seed)
        dossier: dict[str, Any] = {}
        if isinstance(candidate, dict):
            for key, value in candidate.items():
                normalized_key = TOP_LEVEL_ALIASES.get(str(key), str(key))
                if normalized_key in {
                    *DOMAIN_KEYS,
                    *ITEM_DOMAIN_KEYS,
                    *ADVANTAGE_DOMAIN_KEYS,
                }:
                    dossier[normalized_key] = copy.deepcopy(value)
        states = _states_for_payload(
            dossier,
            field_status="user_confirmed",
            origin="user_input",
            source_refs=["seed:structured"],
        )
        return raw, dossier, states
    text = str(seed).strip()
    if not text:
        return "", {}, {}
    if _rich_seed(text):
        dossier, explicit_values = _dossier_from_rich_seed(text)
    else:
        dossier, explicit_values = {}, set()
    states = _states_for_payload(
        dossier,
        field_status="model_proposed",
        origin="model_suggestion",
        decision_status="open",
        source_refs=["model:deterministic-seed-template-v1"],
        confidence=0.7,
    )
    seed_ref = f"seed:{canonical_hash(text)[:16]}"
    for pointer, value in _walk_leaves(dossier):
        if str(value) not in explicit_values:
            continue
        states[pointer] = _field_state(
            field_status="user_confirmed",
            origin="user_input",
            decision_status="session_locked",
            source_refs=[seed_ref],
            confidence=1.0,
        )
    return text, dossier, states


def _rich_seed(text: str) -> bool:
    markers = (
        "题材",
        "读者承诺",
        "规则",
        "稀缺",
        "主角",
        "对手",
        "目标",
        "失败代价",
        "触发事件",
        "第一卷",
        "连载",
        "地点",
        "时间",
    )
    score = sum(marker in text for marker in markers)
    return len(text) >= 500 and score >= 8


def _extract_labeled(text: str, labels: tuple[str, ...], fallback: str) -> str:
    matched = _extract_labeled_optional(text, labels)
    return matched if matched is not None else fallback


def _extract_labeled_optional(
    text: str,
    labels: tuple[str, ...],
) -> str | None:
    for label in labels:
        match = re.search(
            rf"(?:^|[\n；;。])\s*{re.escape(label)}\s*[：:]\s*([^\n；;。]+)",
            text,
        )
        if match:
            return match.group(1).strip()
    return None


def _dossier_from_rich_seed(text: str) -> tuple[dict[str, Any], set[str]]:
    explicit_values: set[str] = set()

    def pick(labels: tuple[str, ...], fallback: str) -> str:
        matched = _extract_labeled_optional(text, labels)
        if matched is not None:
            explicit_values.add(str(matched))
            return matched
        return fallback

    primary = pick(("题材", "主类型"), "复合长篇网文")
    protagonist = pick(("主角",), "种子中定义的主角")
    opponent = pick(("对手", "反派"), "种子中定义的主动对手")
    rule = pick(("核心规则", "世界规则", "规则"), "种子中的核心规则")
    scarcity = pick(("稀缺资源", "稀缺"), "种子中的稀缺资源")
    inciting = pick(("触发事件",), "种子中的不可逆触发事件")
    goal = pick(("外在目标", "目标"), "完成种子中定义的可判定目标")
    failure = pick(("失败代价",), "失去核心关系、资源或身份")
    dossier = {
        "genre_contract": {
            "primary_engine": primary,
            "secondary_engines": [],
            "target_readers": "长篇网文读者",
            "platform_assumptions": "持续连载",
            "reading_promise": pick(
                ("读者承诺", "阅读承诺"),
                "持续升级且每次解决都会改变局面",
            ),
            "recurring_rewards": ["阶段性破局", "认知翻转", "关系与资源变化"],
            "differentiators": [
                pick(("差异化",), "种子中的独特世界约束")
            ],
            "tone": pick(("调性",), "紧张但保留人物温度"),
            "scale_expectation": pick(("规模",), "长篇"),
            "pacing_expectation": "章级反馈、卷级闭环",
            "hard_boundaries": [],
            "anti_promises": [],
        },
        "world_model": {
            "rules": [rule],
            "scarce_resources": [scarcity],
            "power_distribution": pick(
                ("权力分配",), "控制稀缺资源者维护现有秩序"
            ),
            "current_pressures": [
                pick(("当前压力",), "当前平衡正在接近失稳阈值")
            ],
            "mvw": {
                "story_clock": pick(("故事时间", "历法"), "开局日"),
                "locations_and_routes": [
                    "起点地点及本地路线",
                    "关键资源节点",
                    "第一卷冲突节点",
                ],
                "base_rules": [rule],
                "core_capability": {
                    "capability": pick(("核心能力",), "主线相关能力"),
                    "cost": "使用会消耗稀缺资源或暴露痕迹",
                    "boundary": "不能绕过核心规则",
                    "counter": "制度或对手拥有针对手段",
                },
                "survival_resource_chain": "生产者→运输→分配→普通人",
                "power_scarcity_chain": f"{scarcity}→控制权→制度与暴力",
                "daily_cycles": ["普通劳动者的一天", "权力精英的一天"],
                "infrastructure_bottleneck": "关键交通、通信或生产容量有限",
                "formal_institution_chain": "发现→报告→裁决→执行",
                "power_actors": ["秩序维护者", "秩序挑战者"],
                "harmed_group": "承担系统成本的普通群体",
                "legitimacy_narrative": "维持秩序的公开叙事与禁忌",
                "important_secret": "足以改变资源或权力分配的重要秘密",
                "historical_trauma": "仍在塑造制度与关系的历史旧账",
                "pressure_horizons": {
                    "near": "开局危机",
                    "volume": "第一卷制度或资源失衡",
                    "book": "核心规则与权力结构的终局矛盾",
                },
                "irreversible_trigger": inciting,
            },
        },
        "actor_system": {
            "protagonist": {
                "name": protagonist,
                "location": "起点地点",
                "social_position": "受世界压力直接约束的位置",
                "immediate_need": goal,
                "external_goal": goal,
                "long_term_desire": "改变自身与世界的关系",
                "internal_lack": "尚未理解代价与他人立场",
                "values_and_limits": ["保留核心底线"],
                "capabilities": [],
                "resources": [],
                "debts": [],
                "relationships": [],
                "knows": [],
                "suspects": [],
                "misunderstands": [],
                "secrets": [],
                "default_strategy": "优先寻找可控退路再行动",
                "offscreen_plan": None,
                "world_blocker": rule,
            },
            "opponents": [
                {
                    "name": opponent,
                    "goal": "在主角不行动时继续推进自身计划",
                    "resources": [scarcity],
                    "knowledge_boundary": "掌握主角尚不知道的局部信息",
                    "offscreen_plan": "利用当前压力扩大既得利益",
                }
            ],
            "third_parties": [
                {"name": "承受后果的群体", "stake": "生存与合法权益"}
            ],
        },
        "story_engine": {
            "protagonist": protagonist,
            "actionable_goal": goal,
            "inciting_event": inciting,
            "active_opposition": opponent,
            "stakes": "身份、资源、关系与世界局势",
            "failure_cost": failure,
            "world_constraints": [rule],
            "information_asymmetry": "主角、对手与公众掌握的信息不对称",
            "first_event_chain": [inciting, "主角采取自然行动", "规则造成意外结果"],
            "escalation_loop": "成功改变资源或关系，并制造更高层压力",
            "irreversible_state_changes": ["开局平衡被打破"],
            "volume_one_change": pick(
                ("第一卷变化",), "第一卷结束时身份、关系与权力格局不可逆变化"
            ),
            "endgame_direction": "追溯并改变核心规则背后的利益结构",
            "endgame_question": pick(
                ("终局问题",), "主角愿意为改变世界支付什么不可回收的代价"
            ),
        },
        "serialization_contract": {
            "chapter_feedback_loop": "承接变化→形成新选择→付出代价→留下钩子",
            "recurring_reward_types": ["破局", "揭示", "成长", "关系变化"],
            "tension_cycle": "蓄压—行动—预期外结果—新压力",
            "reveal_policy": "信息按行动后果分层释放",
            "hook_policy": "章尾钩子必须来自真实未决状态",
            "promise_windows": [{"promise": "核心谜团", "window": "卷内阶段兑现"}],
            "growth_accounts": ["能力", "资源", "身份", "认知", "关系"],
            "volume_loop": "目标—阻力—变化—新局面",
            "repetition_limits": ["同类反转不能连续换皮"],
            "pacing_guardrails": ["每章至少一个可感知状态变化"],
        },
    }
    return dossier, explicit_values


def _seed_present(seed: Any) -> bool:
    if isinstance(seed, dict):
        return bool(seed)
    return bool(str(seed or "").strip())


def _profile_mode(mode: str, target_profile: str) -> tuple[str, list[dict[str, Any]]]:
    decisions: list[dict[str, Any]] = []
    if mode == "new" and target_profile in {"normalize_only", "continuity_ready"}:
        raise PlotInitError(
            "PROFILE_MODE_MISMATCH",
            f"{mode} does not support target profile {target_profile}",
            mode=mode,
            target_profile=target_profile,
        )
    if mode == "hybrid" and target_profile == "normalize_only":
        decisions.append(
            {
                "decision_id": stable_id("decision", mode, target_profile),
                "kind": "deterministic_normalization",
                "from_mode": "hybrid",
                "to_mode": "ingest",
                "reason": "normalize_only never fills missing content",
            }
        )
        return "ingest", decisions
    return mode, decisions


def create_initial_state(
    *,
    session_id: str,
    workspace_root: Path,
    project_root: Path | None,
    mode: str,
    target_profile: str,
    interaction_profile: str,
    seed: Any,
    source_paths: list[Path],
    expected_canon_revision: int,
    bundle_schema_version: str = PROTOCOL_AUTO,
    session_revision: int = 1,
) -> dict[str, Any]:
    if mode not in ROUTING_MODES:
        raise PlotInitError("INVALID_MODE", f"unsupported initialization mode: {mode}")
    if target_profile not in TARGET_PROFILES:
        raise PlotInitError(
            "INVALID_TARGET_PROFILE",
            f"unsupported target profile: {target_profile}",
        )
    if interaction_profile not in INTERACTION_PROFILES:
        raise PlotInitError(
            "INVALID_INTERACTION_PROFILE",
            f"unsupported interaction profile: {interaction_profile}",
        )
    raw_seed, seed_dossier, seed_states = _normalize_seed(seed)
    try:
        effective_schema = negotiate_initialization_schema(
            bundle_schema_version,
            seed_dossier,
        )
    except ValueError as exc:
        raise PlotInitError(
            "INVALID_INITIALIZATION_SCHEMA",
            str(exc),
            schema_version=bundle_schema_version,
        ) from exc
    now = utc_now()
    return {
        "schema_version": effective_schema,
        "requested_bundle_schema_version": bundle_schema_version,
        "bundle_schema_version": effective_schema,
        "session_id": session_id,
        "requested_mode": mode,
        "mode": None,
        "target_profile": target_profile,
        "interaction_profile": interaction_profile,
        "stage": "CREATED",
        "status": "ACTIVE",
        "workspace_root": str(workspace_root),
        "project_root": str(project_root) if project_root else None,
        "session_revision": session_revision,
        "expected_canon_revision": int(expected_canon_revision),
        "source_snapshot_hash": canonical_hash([]),
        "source_paths": [str(path) for path in source_paths],
        "source_manifest": [],
        "source_diff": {
            "added": [],
            "changed": [],
            "removed": [],
            "unchanged": [],
        },
        "source_issues": [],
        "duplicate_sources": [],
        "claims_by_source": {},
        "source_dossiers": {},
        "source_field_states": {},
        "normalized_exports": {},
        "seed": raw_seed,
        "seed_dossier": seed_dossier,
        "seed_field_states": seed_states,
        "answer_patches": {},
        "answer_field_states": {},
        "answers": {},
        "decisions": [],
        "unknowns": [],
        "conflicts": [],
        "gaps": [],
        "dependency_graph": copy.deepcopy(QUESTION_DEPENDENCIES),
        "invalidated_nodes": [],
        "reprocessed_source_ids": [],
        "dossier": copy.deepcopy(seed_dossier),
        "field_states": copy.deepcopy(seed_states),
        "current_questions": [],
        "question_history": [],
        "decision_package_count": 0,
        "checkpoints": [],
        "bundle": None,
        "bundle_hash": None,
        "proposal_id": None,
        "normalization_roundtrip": None,
        "remote_cache_binding": None,
        "created_at": now,
        "updated_at": now,
    }


def _compose_dossier(state: dict[str, Any]) -> None:
    dossier = copy.deepcopy(state.get("seed_dossier") or {})
    states = copy.deepcopy(state.get("seed_field_states") or {})
    source_dossiers = state.get("source_dossiers") or {}
    source_states = state.get("source_field_states") or {}
    manifest_by_id = {
        str(item.get("source_id")): item for item in state.get("source_manifest") or []
    }
    ordered_source_ids = sorted(
        source_dossiers,
        key=lambda source_id: (
            int(manifest_by_id.get(source_id, {}).get("priority") or 0),
            source_id,
        ),
    )
    for source_id in ordered_source_ids:
        dossier = _deep_merge(dossier, source_dossiers[source_id])
        states.update(copy.deepcopy(source_states.get(source_id) or {}))
    for question_id in sorted(state.get("answer_patches") or {}):
        dossier = _deep_merge(dossier, state["answer_patches"][question_id])
        states.update(copy.deepcopy(state["answer_field_states"].get(question_id) or {}))
    state["dossier"] = dossier
    state["field_states"] = states
    requested = str(
        state.get("requested_bundle_schema_version") or PROTOCOL_AUTO
    )
    try:
        effective = negotiate_initialization_schema(
            requested,
            dossier,
            _claims(state),
        )
    except ValueError as exc:
        raise PlotInitError(
            "INVALID_INITIALIZATION_SCHEMA",
            str(exc),
            schema_version=requested,
        ) from exc
    state["bundle_schema_version"] = effective
    state["schema_version"] = effective


def _structured_source_payload(document: dict[str, Any]) -> dict[str, Any]:
    payload = document.get("_json")
    if not isinstance(payload, dict):
        return {}
    candidate = payload.get("initialization_bundle", payload)
    if not isinstance(candidate, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in candidate.items():
        normalized_key = TOP_LEVEL_ALIASES.get(str(key), str(key))
        if normalized_key in {
            *DOMAIN_KEYS,
            *ITEM_DOMAIN_KEYS,
            *ADVANTAGE_DOMAIN_KEYS,
        }:
            result[normalized_key] = copy.deepcopy(value)
    return result


def _dossier_from_claims(claims: list[dict[str, Any]]) -> dict[str, Any]:
    dossier: dict[str, Any] = {}
    genre: dict[str, Any] = {}
    world: dict[str, Any] = {}
    story: dict[str, Any] = {}
    serialization: dict[str, Any] = {}
    actors: dict[str, Any] = {"protagonist": {}, "opponents": [], "third_parties": []}
    timeline: list[dict[str, Any]] = []
    open_loops: list[dict[str, Any]] = []
    for claim in claims:
        predicate = str(claim.get("predicate") or "")
        value = copy.deepcopy(claim.get("object_or_value"))
        subject = str(claim.get("subject") or "作品")
        if predicate == "genre.primary_engine":
            genre["primary_engine"] = value
        elif predicate == "genre.reading_promise":
            genre["reading_promise"] = value
        elif predicate == "genre.tone":
            genre["tone"] = value
        elif predicate == "genre.differentiator":
            genre.setdefault("differentiators", []).append(value)
        elif predicate == "world.rule":
            world.setdefault("rules", []).append(value)
        elif predicate == "world.scarce_resource":
            world.setdefault("scarce_resources", []).append(value)
        elif predicate == "world.survival_resource":
            world.setdefault("survival_resources", []).append(value)
        elif predicate == "world.pressure":
            world.setdefault("current_pressures", []).append(value)
        elif predicate == "actor.protagonist":
            actors["protagonist"] = {"name": value}
        elif predicate == "actor.opponent":
            actors["opponents"].append({"name": value})
        elif predicate == "actor.third_party":
            actors["third_parties"].append({"name": value})
        elif predicate.startswith("actor."):
            record = actors["protagonist"]
            record.setdefault("name", subject)
            record[predicate.split(".", 1)[1]] = value
        elif predicate == "story.inciting_event":
            story["inciting_event"] = value
        elif predicate == "story.failure_cost":
            story["failure_cost"] = value
        elif predicate == "story.first_event_chain":
            story["first_event_chain"] = value
        elif predicate == "story.volume_one_change":
            story["volume_one_change"] = value
        elif predicate == "story.endgame_question":
            story["endgame_question"] = value
        elif predicate.startswith("serialization."):
            serialization[predicate.split(".", 1)[1]] = value
        elif predicate == "timeline.anchor":
            timeline.append(
                {
                    "event": subject,
                    "story_time": claim.get("story_time") or value,
                    "scope": claim.get("scope"),
                    "source_claim_id": claim.get("claim_id"),
                }
            )
        elif predicate == "open_loop":
            open_loops.append(
                {
                    "loop_id": stable_id("loop", claim.get("claim_id")),
                    "description": value,
                    "status": "open",
                    "source_claim_id": claim.get("claim_id"),
                }
            )
    if genre:
        dossier["genre_contract"] = genre
    if world:
        dossier["world_model"] = world
    if actors["protagonist"] or actors["opponents"] or actors["third_parties"]:
        dossier["actor_system"] = actors
    if story:
        dossier["story_engine"] = story
    if serialization:
        dossier["serialization_contract"] = serialization
    if timeline:
        dossier["timeline"] = timeline
    if open_loops:
        dossier["open_loops"] = open_loops
    return dossier


def refresh_sources(
    state: dict[str, Any],
    *,
    remote_cache: "RemoteResponseCache | None" = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Refresh source heads and only re-extract changed source versions."""

    source_paths = [Path(value) for value in state.get("source_paths") or []]
    previous_manifest = list(state.get("source_manifest") or [])
    result = inventory_sources(
        source_paths,
        previous_manifest=previous_manifest,
        remote_cache=remote_cache,
    )
    documents_by_id = {
        str(item["source_id"]): item for item in result.get("documents") or []
    }
    changed_ids = set(result["source_diff"]["added"])
    changed_ids.update(
        str(item["source_id"]) for item in result["source_diff"]["changed"]
    )
    removed_ids = set(result["source_diff"]["removed"])
    claims_by_source = copy.deepcopy(state.get("claims_by_source") or {})
    source_dossiers = copy.deepcopy(state.get("source_dossiers") or {})
    source_field_states = copy.deepcopy(state.get("source_field_states") or {})
    normalized_exports = copy.deepcopy(state.get("normalized_exports") or {})

    for source_id in removed_ids:
        claims_by_source.pop(source_id, None)
        source_dossiers.pop(source_id, None)
        source_field_states.pop(source_id, None)
        normalized_exports.pop(source_id, None)

    reprocessed: list[str] = []
    for source_id in sorted(changed_ids):
        document = documents_by_id.get(source_id)
        if document is None:
            continue
        normalized_bundle = parse_normalized_export(document.get("_json"))
        claims = extract_claims(document, remote_cache=remote_cache)
        source_payload = _deep_merge(
            _dossier_from_claims(claims),
            _structured_source_payload(document),
        )
        claims_by_source[source_id] = claims
        source_dossiers[source_id] = source_payload
        descriptor = next(
            (
                item
                for item in result["source_manifest"]
                if str(item["source_id"]) == source_id
            ),
            {},
        )
        if isinstance(document.get("remote_claim_review"), dict):
            descriptor["remote_claim_review"] = copy.deepcopy(
                document["remote_claim_review"]
            )
        if normalized_bundle is not None:
            embedded_states = normalized_bundle.get("field_states")
            source_field_states[source_id] = (
                copy.deepcopy(embedded_states)
                if isinstance(embedded_states, dict)
                else {}
            )
            normalized_exports[source_id] = {
                "bundle": normalized_bundle,
                "normalization_hash": normalized_hash(normalized_bundle),
                "transport_source_hash": descriptor.get("content_hash"),
                "transport_source_version_id": descriptor.get("source_version_id"),
            }
        else:
            normalized_exports.pop(source_id, None)
            source_field_states[source_id] = _states_for_payload(
                source_payload,
                field_status=(
                    "source_supported"
                    if descriptor.get("authority_tier") in {"T0", "T1", "T2", "T3"}
                    else "model_proposed"
                ),
                origin="source_extract",
                decision_status="open",
                source_refs=[
                    str(claim["claim_id"]) for claim in claims[:128]
                ]
                or [str(descriptor.get("source_version_id") or source_id)],
                confidence=float(descriptor.get("classification_confidence") or 0.5),
                scope=(
                    "planned"
                    if descriptor.get("scope_policy") == "planned_only"
                    else None
                ),
                knowledge_plane=(
                    "author_plan"
                    if descriptor.get("source_role") in {"outline", "note"}
                    else "objective"
                ),
                branch_id=str(descriptor.get("branch_id") or "main"),
            )
        reprocessed.append(source_id)

    state["source_manifest"] = result["source_manifest"]
    state["source_snapshot_hash"] = result["snapshot_hash"]
    state["source_diff"] = result["source_diff"]
    state["source_issues"] = result["issues"]
    state["duplicate_sources"] = result["duplicates"]
    state["claims_by_source"] = claims_by_source
    state["source_dossiers"] = source_dossiers
    state["source_field_states"] = source_field_states
    state["normalized_exports"] = normalized_exports
    state["reprocessed_source_ids"] = reprocessed
    invalidated: list[str] = []
    for source_id in sorted(changed_ids | removed_ids):
        invalidated.extend(
            [
                f"source:{source_id}:extract",
                f"source:{source_id}:conflict",
            ]
        )
    if changed_ids or removed_ids:
        invalidated.extend(["gap", "normalize", "validate", "proposal"])
    state["invalidated_nodes"] = invalidated
    _compose_dossier(state)
    return result, result.get("documents") or []


def _has_effective_sources(state: dict[str, Any]) -> bool:
    return any(
        item.get("parse_status") == "parsed"
        and item.get("ingest_policy") != "exclude"
        for item in state.get("source_manifest") or []
    )


def _route_mode(state: dict[str, Any]) -> None:
    requested = str(state.get("requested_mode") or "auto")
    if requested == "auto":
        has_sources = _has_effective_sources(state)
        has_seed = _seed_present(state.get("seed"))
        selected = "hybrid" if has_sources and has_seed else "ingest" if has_sources else "new"
    else:
        selected = requested
    selected, normalization_decisions = _profile_mode(
        selected, str(state["target_profile"])
    )
    state["mode"] = selected
    state["decisions"].extend(normalization_decisions)


def _genre_sufficient(dossier: dict[str, Any]) -> bool:
    value = dossier.get("genre_contract")
    if not isinstance(value, dict):
        return False
    required = ("primary_engine", "reading_promise", "recurring_rewards", "differentiators", "tone")
    return all(bool(value.get(key)) for key in required)


def _world_sufficient(dossier: dict[str, Any]) -> bool:
    value = dossier.get("world_model")
    if not isinstance(value, dict):
        return False
    mvw = value.get("mvw")
    if isinstance(mvw, dict):
        present = sum(bool(mvw.get(key)) for key in MVW_FIELDS)
        return present >= 12
    return all(
        bool(value.get(key))
        for key in ("rules", "scarce_resources", "power_distribution", "current_pressures")
    )


def _actor_sufficient(dossier: dict[str, Any]) -> bool:
    value = dossier.get("actor_system")
    if not isinstance(value, dict):
        return False
    protagonist = value.get("protagonist")
    opponents = value.get("opponents")
    third_parties = value.get("third_parties")
    return bool(protagonist) and bool(opponents) and bool(third_parties)


def _story_sufficient(dossier: dict[str, Any]) -> bool:
    value = dossier.get("story_engine")
    if not isinstance(value, dict):
        return False
    required = (
        "actionable_goal",
        "inciting_event",
        "active_opposition",
        "failure_cost",
        "first_event_chain",
        "volume_one_change",
        "endgame_question",
    )
    return all(bool(value.get(key)) for key in required)


def _serialization_sufficient(dossier: dict[str, Any]) -> bool:
    value = dossier.get("serialization_contract")
    if not isinstance(value, dict):
        return False
    return all(
        bool(value.get(key))
        for key in ("chapter_feedback_loop", "recurring_reward_types", "volume_loop")
    )


def _power_report(state: dict[str, Any]) -> dict[str, Any]:
    package = build_power_package(
        state.get("dossier") or {},
        _claims(state),
        mode=str(state.get("mode") or "new"),
    )
    return {
        "package": package,
        "sufficiency": power_sufficiency(
            package,
            mode=str(state.get("mode") or "new"),
        ),
    }


def _seed_hint(state: dict[str, Any]) -> str:
    seed = state.get("seed")
    if isinstance(seed, str) and seed.strip():
        return seed.strip()[:160]
    genre = (state.get("dossier") or {}).get("genre_contract") or {}
    if isinstance(genre, dict) and genre.get("primary_engine"):
        return str(genre["primary_engine"])
    return "这部作品"


def _genre_options(state: dict[str, Any]) -> list[dict[str, Any]]:
    hint = _seed_hint(state)
    candidates = [
        (
            "pressure-growth",
            "压力升级型",
            "以持续变强、资源竞争和阶段破局为主回报，世界会围绕能力成本与阶层通道展开。",
            {
                "primary_engine": f"{hint}：压力升级与生存破局",
                "secondary_engines": ["资源竞争"],
                "target_readers": "偏好升级、危机与连续回报的网文读者",
                "platform_assumptions": "长篇连载",
                "reading_promise": "主角每次破局都会获得可感知成长，同时制造更高层压力",
                "recurring_rewards": ["能力成长", "资源获取", "身份跃迁", "反制强敌"],
                "differentiators": ["成长必须支付可追踪代价"],
                "tone": "紧张、克制、持续升级",
                "scale_expectation": "长篇",
                "pacing_expectation": "章级反馈、卷级不可逆变化",
                "hard_boundaries": [],
                "anti_promises": ["无代价碾压"],
            },
        ),
        (
            "mystery-discovery",
            "谜团探索型",
            "以信息差、世界秘密和认知翻转为主回报，世界会强化传播障碍与证据链。",
            {
                "primary_engine": f"{hint}：谜团探索与认知翻转",
                "secondary_engines": ["生存危机"],
                "target_readers": "偏好世界秘密、推理与反转的网文读者",
                "platform_assumptions": "长篇连载",
                "reading_promise": "每轮行动揭开一层真相，同时暴露更危险的未知",
                "recurring_rewards": ["线索闭合", "认知翻转", "秘密揭示", "立场变化"],
                "differentiators": ["秘密会真实改变人物可行行动"],
                "tone": "悬疑、危险、逐层扩展",
                "scale_expectation": "长篇",
                "pacing_expectation": "短线谜团兑现、长线真相递进",
                "hard_boundaries": [],
                "anti_promises": ["只靠旁白解释真相"],
            },
        ),
        (
            "institution-conflict",
            "制度冲突型",
            "以身份、规则、组织博弈和利益重排为主回报，世界会强化制度执行链与普通人生活。",
            {
                "primary_engine": f"{hint}：制度冲突与利益重排",
                "secondary_engines": ["人物成长"],
                "target_readers": "偏好城市社会、组织博弈与因果推进的网文读者",
                "platform_assumptions": "长篇连载",
                "reading_promise": "主角每次解决危机都会改变关系、规则或利益分配",
                "recurring_rewards": ["规则破局", "联盟变化", "身份跃迁", "制度反噬"],
                "differentiators": ["世界制度能阻断最自然的解决方案"],
                "tone": "现实质感、紧张、带人物温度",
                "scale_expectation": "长篇",
                "pacing_expectation": "事件链闭环推动格局扩展",
                "hard_boundaries": [],
                "anti_promises": ["组织像高属性普通人一样静止"],
            },
        ),
    ]
    limit = 2 if state["interaction_profile"] == "minimal" else 3
    return [
        {"option_id": option_id, "label": label, "impact": impact, "patch": {"genre_contract": patch}}
        for option_id, label, impact, patch in candidates[:limit]
    ]


def _world_options(state: dict[str, Any]) -> list[dict[str, Any]]:
    genre = (state.get("dossier") or {}).get("genre_contract") or {}
    engine = str(genre.get("primary_engine") or _seed_hint(state))

    def patch(
        rule: str,
        scarce: str,
        power: str,
        pressure: str,
        capability: str,
    ) -> dict[str, Any]:
        return {
            "world_model": {
                "rules": [rule],
                "scarce_resources": [scarce],
                "power_distribution": power,
                "current_pressures": [pressure],
                "causal_kernel": [
                    rule,
                    f"{scarce}形成稀缺",
                    power,
                    pressure,
                    "人物自然行动被规则与利益结构阻断",
                ],
                "mvw": {
                    "story_clock": "开局日；后续需绑定作品历法",
                    "locations_and_routes": [
                        "主角起点",
                        "关键资源节点",
                        "第一卷冲突节点",
                    ],
                    "base_rules": [rule],
                    "core_capability": {
                        "capability": capability,
                        "cost": f"消耗{scarce}并留下可追踪痕迹",
                        "boundary": "不能绕过基础规则或无限叠加",
                        "counter": "制度封锁、资源断供与同类反制",
                    },
                    "survival_resource_chain": "生产者→交通瓶颈→配给与市场→家庭",
                    "power_scarcity_chain": f"{scarce}→资格与组织控制→暴力和合法性",
                    "daily_cycles": ["普通劳动者的一天", "资源控制者的一天"],
                    "infrastructure_bottleneck": "关键交通或通信容量有限且可被封锁",
                    "formal_institution_chain": "发现→报告→裁决→执行→申诉或规避",
                    "power_actors": ["规则维护者", "既得利益挑战者"],
                    "harmed_group": "承担资源短缺与执法成本的普通群体",
                    "legitimacy_narrative": "秩序以安全和稀缺管理证明自身正当",
                    "important_secret": "资源稀缺或规则例外的真实来源",
                    "historical_trauma": "一次资源灾难塑造了当前制度与禁忌",
                    "pressure_horizons": {
                        "near": pressure,
                        "volume": "关键资源链失衡并触发组织冲突",
                        "book": "核心规则、资源来源与合法性全面冲突",
                    },
                    "irreversible_trigger": "开局事件打破资源与信息的旧平衡",
                },
            }
        }

    candidates = [
        (
            "scarcity-order",
            "稀缺资源秩序",
            "适合强调生存、成长与阶层流动；所有能力都受资源链和配给制度约束。",
            patch(
                f"{engine}中的核心能力必须消耗稀缺资源，无法无痕使用",
                "可储存但产量受限的核心资源",
                "维护者控制生产、认证、交通与配给，挑战者争夺黑市和替代来源",
                "近期断供正在扩大阶层冲突",
                "对资源进行转化或借用规则的能力",
            ),
        ),
        (
            "information-order",
            "信息与资格秩序",
            "适合强调谜团、调查和身份；权力来自谁能验证真相、授予资格与控制传播。",
            patch(
                "关键知识只有满足身份、地点和代价条件才能被验证",
                "可信信息与合法资格",
                "认证机构控制档案、通信和解释权，地下网络交易残缺真相",
                "一条秘密正在突破传播封锁",
                "读取或验证隐藏信息的能力",
            ),
        ),
        (
            "mobility-order",
            "交通与边界秩序",
            "适合强调城市、地域与组织博弈；移动、物流和通信的容量直接决定权力。",
            patch(
                "跨区域移动必须依赖受控基础设施并支付时间、资格与暴露成本",
                "通行资格与运输容量",
                "交通维护者控制路线和时刻，边缘群体依赖走私与临时通道",
                "关键路线即将封闭或改制",
                "短暂改变路线、通行条件或旅时的能力",
            ),
        ),
    ]
    limit = 2 if state["interaction_profile"] == "minimal" else 3
    return [
        {"option_id": option_id, "label": label, "impact": impact, "patch": value}
        for option_id, label, impact, value in candidates[:limit]
    ]


def _power_options(state: dict[str, Any]) -> list[dict[str, Any]]:
    dossier = state.get("dossier") or {}
    detected = build_power_package(
        dossier,
        _claims(state),
        mode=str(state.get("mode") or "new"),
    )
    detected_profile = str(
        ((detected.get("power_systems") or [{}])[0]).get("profile") or "mundane"
    )
    if not (
        dossier.get("power_profile")
        or dossier.get("power_system")
        or dossier.get("power_systems")
    ):
        candidate_profiles = ["cultivation", "magic", "mundane"]
    else:
        candidate_profiles = [detected_profile, "hybrid", "mundane"]
    seen: set[str] = set()
    profiles = [
        profile
        for profile in candidate_profiles
        if not (profile in seen or seen.add(profile))
    ]
    labels = {
        "cultivation": "修仙成长核",
        "magic": "魔法施法核",
        "skill_tree": "技能树成长核",
        "game": "游戏升级核",
        "martial": "武道成长核",
        "superpower": "异能负荷核",
        "bloodline": "血脉觉醒核",
        "technology": "科技强化核",
        "contract_summoning": "契约召唤核",
        "system_assist": "系统辅助核",
        "hybrid": "多体系隔离核",
        "mundane": "无超凡声明",
    }
    native = {
        "cultivation": ("修为", "灵力", "本命术式", "师承与功法"),
        "magic": ("施法者成长", "法力", "核心法术", "学派与传承"),
        "skill_tree": ("技能熟练", "训练时长", "核心技能", "训练与导师"),
        "game": ("角色成长", "经验", "核心技能", "职业与任务"),
        "martial": ("武道修为", "气血", "代表招式", "师承与训练"),
        "superpower": ("觉醒控制", "精神负荷", "觉醒能力", "觉醒事件"),
        "bloodline": ("血脉阶段", "血脉负荷", "血脉能力", "遗传与仪式"),
        "technology": ("强化档位", "能源", "核心模块", "装备与授权"),
        "contract_summoning": ("契约成长", "精神负担", "召唤能力", "契约对象"),
        "system_assist": ("宿主权限", "积分", "系统模块", "任务与系统"),
        "hybrid": ("各体系独立成长", "各体系独立资源", "跨体系能力", "显式来源"),
    }

    def patch(profile: str) -> dict[str, Any]:
        if profile == "mundane":
            return {
                "power_profile": "mundane",
                "power_systems": [
                    {
                        "namespace": "project.mundane",
                        "name": "现实技能与社会资格",
                        "profile": "mundane",
                        "model_status": "not_applicable",
                        "no_resource_pool": True,
                        "social_consequences": ["职业资历与社会权限改变可行动空间"],
                    }
                ],
                "progression_tracks": [],
                "rank_nodes": [],
                "rank_edges": [],
                "ability_definitions": [],
                "resource_definitions": [],
                "status_definitions": [],
                "qualification_definitions": [],
                "counter_rules": [],
                "bridge_rules": [],
                "conversion_rules": [],
                "actor_power_bootstrap": [],
            }
        track, resource, ability, source = native[profile]
        namespace = f"project.{profile}"
        return {
            "power_profile": profile,
            "power_systems": [
                {
                    "namespace": namespace,
                    "name": labels[profile],
                    "profile": profile,
                    "model_status": "modeled",
                    "cross_system_policy": (
                        "isolated" if profile == "hybrid" else "unknown"
                    ),
                    "social_consequences": [
                        "力量入口受组织、资格或资源控制并改变身份与日常选择"
                    ],
                }
            ],
            "progression_tracks": [
                {
                    "namespace": f"{namespace}.main",
                    "name": track,
                    "track_kind": "open_ended",
                }
            ],
            "rank_nodes": [],
            "rank_edges": [],
            "ability_definitions": [
                {
                    "name": ability,
                    "ability_kind": "active",
                    "source_bindings": [source],
                    "effects": ["在第一卷核心危机中提供有限破局手段"],
                    "costs": [f"消耗{resource}并留下可追踪痕迹"],
                    "conditions": ["满足当前成长阶段与来源绑定"],
                    "limits": ["不能绕过世界基础规则或无限连续使用"],
                    "counters": ["资源断供、来源失效或对手已建立的针对手段"],
                }
            ],
            "resource_definitions": [
                {
                    "name": resource,
                    "resource_kind": "stock",
                    "acquisition": ["训练、任务或受控资源链"],
                    "consumption": ["能力使用与成长尝试"],
                    "recovery": ["故事时间推进、休整或明确补给"],
                    "capacity": "受当前成长阶段限制",
                }
            ],
            "status_definitions": [],
            "qualification_definitions": [],
            "counter_rules": [],
            "bridge_rules": [],
            "conversion_rules": [],
            "actor_power_bootstrap": [],
        }

    impacts = {
        "mundane": "明确跳过超凡境界、法力池和战力换算，只保留现实技能、资格与资源。",
        "hybrid": "多命名空间并行；没有 accepted BridgeRule 时不进行等级或资源换算。",
    }
    options = []
    for profile in profiles:
        options.append(
            {
                "option_id": f"power-{profile}",
                "label": labels[profile],
                "impact": impacts.get(
                    profile,
                    "建立来源—成长—资源—代价—反制—社会后果的第一卷力量因果核。",
                ),
                "patch": patch(profile),
            }
        )
    return options[: (2 if state["interaction_profile"] == "minimal" else 3)]


def _story_options(state: dict[str, Any]) -> list[dict[str, Any]]:
    world = (state.get("dossier") or {}).get("world_model") or {}
    rules = world.get("rules") or ["核心世界规则"]
    scarce = (world.get("scarce_resources") or ["关键资源"])[0]
    protagonist_name = _extract_labeled(_seed_hint(state), ("主角",), "主角")

    def patch(goal: str, inciting: str, opponent: str, cost: str) -> dict[str, Any]:
        return {
            "actor_system": {
                "protagonist": {
                    "name": protagonist_name,
                    "identity": "受当前压力直接约束的行动者",
                    "location": "主角起点",
                    "social_position": "缺乏完整资格与信息",
                    "immediate_need": goal,
                    "external_goal": goal,
                    "long_term_desire": "获得决定自身道路的能力",
                    "internal_lack": "低估规则背后的利益网络",
                    "values_and_limits": ["保留可回头的退路", "不主动牺牲无辜者"],
                    "capabilities": [],
                    "resources": [scarce],
                    "debts": [],
                    "relationships": [],
                    "knows": [],
                    "suspects": [],
                    "misunderstands": [],
                    "secrets": [],
                    "default_strategy": "先保全自身和退路，再寻找最小代价破局",
                    "offscreen_plan": None,
                    "world_blocker": rules[0],
                },
                "opponents": [
                    {
                        "name": opponent,
                        "goal": "维持或扩大对关键资源、资格与解释权的控制",
                        "resources": [scarce, "制度执行链", "信息优势"],
                        "knowledge_boundary": "知道局部秘密但误判主角底牌",
                        "default_strategy": "先封锁选择，再迫使主角进入可控路径",
                        "offscreen_plan": "即使主角不行动也会推进封锁与清算",
                    }
                ],
                "third_parties": [
                    {
                        "name": "承受后果的本地群体",
                        "stake": "生存资源、通行权与免受清算",
                        "response": "会根据代价在合作、观望和出卖之间变化",
                    }
                ],
            },
            "story_engine": {
                "protagonist": protagonist_name,
                "actionable_goal": goal,
                "inciting_event": inciting,
                "active_opposition": opponent,
                "stakes": "身份、资源、关系与第一卷局势",
                "failure_cost": cost,
                "world_constraints": rules,
                "information_asymmetry": "主角不知道资源与规则的完整来源，对手不知道主角的真实选择边界",
                "first_event_chain": [
                    inciting,
                    "主角采取最自然的自保行动",
                    "世界规则使方案产生预期外代价",
                    "对手利用代价主动收紧局面",
                    "主角作出改变资源或关系的不可逆选择",
                ],
                "escalation_loop": "局部成功→资源/身份变化→对手学习→更高层规则介入",
                "irreversible_state_changes": ["旧身份或旧关系失效", "对手确认主角为威胁"],
                "volume_one_change": "第一卷结束时主角进入新的身份层级，资源链和敌我格局不可恢复",
                "endgame_direction": "追溯并改变核心规则、稀缺资源与合法性叙事之间的关系",
                "endgame_question": "主角能否改变秩序而不成为新一轮垄断者",
            },
            "serialization_contract": {
                "chapter_feedback_loop": "承接上章变化→形成选择→支付代价→留下真实未决状态",
                "recurring_reward_types": ["破局", "资源变化", "关系变化", "信息揭示"],
                "tension_cycle": "蓄压—行动—预期外结果—反制—新选择",
                "reveal_policy": "只在人物行动获得证据后释放关键信息",
                "hook_policy": "章尾钩子必须绑定期限、对手行动或不可逆变化",
                "promise_windows": [{"promise": "当前核心疑问", "window": "3—10章"}],
                "growth_accounts": ["能力", "资源", "身份", "认知", "关系"],
                "volume_loop": "明确目标—渐进困境—危机选择—高潮兑现—新局面",
                "repetition_limits": ["连续两次不得使用同构误会或救场"],
                "pacing_guardrails": ["每章至少改变一项可检索状态"],
            },
        }

    candidates = [
        (
            "forced-choice",
            "被迫选择",
            "开局立即让主角在两个都有代价的选项中行动，适合高压快节奏。",
            patch(
                "在期限前保住自身与关键第三方的生存资格",
                "主角被卷入一次资源、资格或秘密失窃事件，旧退路同时失效",
                "控制关键资源并主动清算泄漏者的组织执行者",
                "失去身份与退路，第三方遭到连带清算",
            ),
        ),
        (
            "opportunity-trap",
            "机会陷阱",
            "主角主动抓住看似可控的机会，成功本身暴露更高层风险。",
            patch(
                "利用一次稀缺机会完成身份跃迁并保住收益",
                "主角得到一份足以改变命运、却违反核心规则的资源或资格",
                "试图回收资源并掩盖制度漏洞的既得利益者",
                "收益被夺、秘密暴露，并被永久列入追索名单",
            ),
        ),
        (
            "third-party-crisis",
            "第三方危机",
            "通过受影响群体把世界压力转成行动，适合强调关系和制度后果。",
            patch(
                "在不彻底暴露底牌的前提下阻止一场针对本地群体的清算",
                "资源断供或规则改制让第三方在七天内失去生存条件",
                "把危机视为整顿、兼并或试验机会的权力行动者",
                "第三方崩溃、主角信誉破产，关键资源链被对手独占",
            ),
        ),
    ]
    limit = 2 if state["interaction_profile"] == "minimal" else 3
    return [
        {"option_id": option_id, "label": label, "impact": impact, "patch": value}
        for option_id, label, impact, value in candidates[:limit]
    ]


def _question_package(state: dict[str, Any], question_id: str) -> dict[str, Any]:
    if question_id == "genre-contract":
        prompt = "选择最能代表持续阅读动力的题材合同。"
        stage = "GENRE_CONTRACT"
        options = _genre_options(state)
    elif question_id == "world-causal-kernel":
        prompt = "选择世界因果核；它会决定资源、权力、日常生活与主线阻力。"
        stage = "WORLD_CAUSAL_KERNEL"
        options = _world_options(state)
    elif question_id == "power-causal-kernel":
        prompt = "选择力量因果核；它会明确来源、成长轨、资源循环、能力边界、反制与社会后果。"
        stage = "POWER_CAUSAL_KERNEL"
        options = _power_options(state)
    else:
        prompt = "选择第一条可启动的剧情发动机；人物、对手与连载合同会一起落位。"
        stage = "STORY_ENGINE"
        options = _story_options(state)
    return {
        "question_id": question_id,
        "stage": stage,
        "expected_session_revision": int(state["session_revision"]),
        "prompt": prompt,
        "answer_format": "option_id | natural_language | structured_patch",
        "options": options,
        "default_option_id": options[0]["option_id"],
    }


def _need_question(state: dict[str, Any], question_id: str) -> None:
    package = _question_package(state, question_id)
    state["stage"] = package["stage"]
    state["status"] = "NEEDS_INPUT"
    state["current_questions"] = [package]
    if question_id not in state["question_history"]:
        state["question_history"].append(question_id)
        state["decision_package_count"] = len(state["question_history"])


def _derive_serialization(state: dict[str, Any]) -> None:
    if _serialization_sufficient(state["dossier"]):
        return
    patch = {
        "serialization_contract": {
            "chapter_feedback_loop": "承接状态→形成选择→支付代价→留下未决变化",
            "recurring_reward_types": ["行动结果", "信息揭示", "关系或资源变化"],
            "tension_cycle": "蓄压—行动—结果—新压力",
            "reveal_policy": "信息由人物行动和证据释放",
            "hook_policy": "章尾钩子绑定真实期限、对手计划或不可逆变化",
            "promise_windows": [],
            "growth_accounts": ["资源", "能力", "身份", "认知", "关系"],
            "volume_loop": "目标—阻力—变化—新局面",
            "repetition_limits": ["避免同构冲突连续换皮"],
            "pacing_guardrails": ["章级至少一个可感知反馈"],
        }
    }
    state["answer_patches"].setdefault("derived-serialization", patch)
    state["answer_field_states"].setdefault(
        "derived-serialization",
        _states_for_payload(
            patch,
            field_status="model_proposed",
            origin="deterministic_derived",
            decision_status="open",
            confidence=0.7,
            depends_on=["story_engine"],
        ),
    )
    _compose_dossier(state)


def _claims(state: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for source_id in sorted(state.get("claims_by_source") or {}):
        values.extend(copy.deepcopy(state["claims_by_source"][source_id]))
    return values


def _conflicts(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_field: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for claim in claims:
        key = (
            str(claim.get("subject") or ""),
            str(claim.get("predicate") or ""),
            str(claim.get("branch_id") or "main"),
        )
        by_field[key].append(claim)
    conflicts: list[dict[str, Any]] = []
    for key, grouped in sorted(by_field.items()):
        values: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for claim in grouped:
            values[canonical_json(claim.get("object_or_value"))].append(claim)
        if len(values) <= 1:
            if len(grouped) > 1:
                conflicts.append(
                    {
                        "conflict_id": stable_id("conflict", key, "duplicate"),
                        "type": "duplicate_fact",
                        "severity": "info",
                        "subject": key[0],
                        "predicate": key[1],
                        "claim_ids": [claim["claim_id"] for claim in grouped],
                        "candidate_values": [
                            copy.deepcopy(grouped[0].get("object_or_value"))
                        ],
                        "resolution_status": "merge_evidence",
                        "requires_user_input": False,
                    }
                )
            continue
        scopes = {claim.get("scope") for claim in grouped}
        times = {
            canonical_json(claim.get("story_time"))
            for claim in grouped
            if claim.get("story_time")
        }
        modalities = {claim.get("modality") for claim in grouped}
        if len(times) > 1:
            conflict_type = "temporal_evolution"
            severity = "review"
            requires_input = False
        elif "planned" in scopes and "current" in scopes:
            conflict_type = "planned_current_mismatch"
            severity = "high"
            requires_input = True
        elif modalities & {"hypothetical", "conditional"}:
            conflict_type = "alternative_or_conditional"
            severity = "review"
            requires_input = False
        else:
            conflict_type = "semantic_contradiction"
            severity = "high"
            requires_input = True
        conflicts.append(
            {
                "conflict_id": stable_id("conflict", key, sorted(values)),
                "type": conflict_type,
                "severity": severity,
                "subject": key[0],
                "predicate": key[1],
                "claim_ids": [claim["claim_id"] for claim in grouped],
                "candidate_values": [
                    copy.deepcopy(group[0].get("object_or_value"))
                    for _, group in sorted(values.items())
                ],
                "candidate_sources": [
                    {
                        "claim_id": claim["claim_id"],
                        "source_id": claim["source_id"],
                        "authority_tier": claim["authority_tier"],
                        "scope": claim.get("scope"),
                    }
                    for claim in grouped
                ],
                "resolution_status": "open",
                "allowed_operations": [
                    "choose_source",
                    "temporalize",
                    "assign_branch",
                    "supersede",
                    "retract",
                    "author_value",
                    "defer_and_exclude",
                ],
                "requires_user_input": requires_input,
            }
        )
    return conflicts


def _gap(
    path: str,
    category: str,
    description: str,
    *,
    hard: bool,
    mode: str,
) -> dict[str, Any]:
    return {
        "gap_id": stable_id("gap", path, category),
        "path": path,
        "category": category,
        "description": description,
        "severity": "hard" if hard else "deferred",
        "blocks_proposal": hard and mode in {"new", "hybrid"},
        "requires_user_input": hard and mode == "hybrid",
        "safe_to_defer": not hard,
    }


def _gaps(state: dict[str, Any]) -> list[dict[str, Any]]:
    dossier = state["dossier"]
    mode = str(state["mode"])
    profile = str(state["target_profile"])
    if profile == "normalize_only":
        hard = False
    else:
        hard = True
    gaps: list[dict[str, Any]] = []
    if not _genre_sufficient(dossier):
        gaps.append(
            _gap(
                "/genre_contract",
                "genre_contract",
                "题材发动机、读者承诺或持续回报尚不完整",
                hard=hard,
                mode=mode,
            )
        )
    if not _world_sufficient(dossier):
        gaps.append(
            _gap(
                "/world_model",
                "world_causal_kernel",
                "规则、稀缺资源、权力分配、日常运行或压力链尚不完整",
                hard=hard,
                mode=mode,
            )
        )
    if state.get("bundle_schema_version") == PROTOCOL_V2:
        power_report = _power_report(state)["sufficiency"]
        if not power_report["sufficient"]:
            gaps.append(
                _gap(
                    "/power_model",
                    "power_causal_kernel",
                    "力量因果核尚不完整："
                    + "、".join(power_report["blocking_checks"]),
                    hard=hard,
                    mode=mode,
                )
            )
    if not _actor_sufficient(dossier):
        gaps.append(
            _gap(
                "/actor_system",
                "actor_anchor",
                "主角、主动对手或受影响第三方尚未形成可行动锚点",
                hard=hard,
                mode=mode,
            )
        )
    if not _story_sufficient(dossier):
        gaps.append(
            _gap(
                "/story_engine",
                "story_engine",
                "触发事件、可判定目标、主动阻力、失败代价或第一卷变化尚不完整",
                hard=hard,
                mode=mode,
            )
        )
    if not _serialization_sufficient(dossier):
        gaps.append(
            _gap(
                "/serialization_contract",
                "serialization",
                "章级反馈、持续回报或卷级循环尚不完整",
                hard=hard,
                mode=mode,
            )
        )
    if profile == "world_bible":
        world = dossier.get("world_model") or {}
        if not isinstance(world, dict) or not world.get("regional"):
            gaps.append(
                _gap(
                    "/world_model/resolution/regional",
                    "world_bible_depth",
                    "世界圣经档位尚未展开区域层结构",
                    hard=False,
                    mode=mode,
                )
            )
    if profile == "continuity_ready":
        if not state.get("source_manifest"):
            gaps.append(
                _gap(
                    "/source_manifest",
                    "continuity_restore",
                    "连续性恢复档位缺少可追踪来源",
                    hard=True,
                    mode=mode,
                )
            )
        if not (dossier.get("timeline") or []):
            gaps.append(
                _gap(
                    "/timeline",
                    "continuity_restore",
                    "连续性恢复档位缺少故事时间锚点",
                    hard=True,
                    mode=mode,
                )
            )
    return gaps


def _entities_and_aliases(claims: list[dict[str, Any]], dossier: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    alias_records: list[dict[str, Any]] = []

    def add(name: str, entity_type: str, source_refs: list[str] | None = None) -> str:
        cleaned = name.strip()
        key = (entity_type, cleaned.casefold())
        entity_id = stable_id("ent", entity_type, cleaned.casefold())
        candidates.setdefault(
            key,
            {
                "entity_id": entity_id,
                "entity_type": entity_type,
                "canonical_name": cleaned,
                "aliases": [],
                "resolution_status": "resolved",
                "source_refs": list(source_refs or []),
            },
        )
        for source_ref in source_refs or []:
            if source_ref not in candidates[key]["source_refs"]:
                candidates[key]["source_refs"].append(source_ref)
        return entity_id

    actors = dossier.get("actor_system") or {}
    if isinstance(actors, dict):
        protagonist = actors.get("protagonist")
        if isinstance(protagonist, dict) and protagonist.get("name"):
            add(str(protagonist["name"]), "character", ["dossier:actor_system"])
        for key in ("opponents", "third_parties"):
            for item in actors.get(key) or []:
                if isinstance(item, dict) and item.get("name"):
                    add(str(item["name"]), "character", ["dossier:actor_system"])

    for claim in claims:
        subject = str(claim.get("subject") or "").strip()
        if subject and subject not in {"作品", "故事时间"}:
            entity_type = entity_type_for_claim(claim)
            if entity_type in {"contract", "plot", "time"}:
                entity_type = "concept"
            entity_id = add(subject, entity_type, [str(claim["claim_id"])])
        else:
            entity_id = ""
        if claim.get("predicate") == "entity.alias":
            aliases = claim.get("object_or_value")
            if isinstance(aliases, str):
                aliases = [aliases]
            for alias in aliases or []:
                alias_text = str(alias).strip()
                if not alias_text:
                    continue
                alias_id = stable_id("alias", entity_id, alias_text.casefold())
                alias_records.append(
                    {
                        "alias_id": alias_id,
                        "alias": alias_text,
                        "entity_id": entity_id,
                        "source_claim_id": claim["claim_id"],
                        "confidence": claim.get("confidence", 0.5),
                        "resolution_status": (
                            "resolved"
                            if float(claim.get("confidence") or 0) >= 0.8
                            else "AMBIGUOUS"
                        ),
                    }
                )
                for entity in candidates.values():
                    if entity["entity_id"] == entity_id and alias_text not in entity["aliases"]:
                        entity["aliases"].append(alias_text)
        elif claim.get("predicate") == "relation":
            relation = claim.get("object_or_value")
            target = (
                str(relation.get("target") or "").strip()
                if isinstance(relation, dict)
                else ""
            )
            if target:
                add(target, "character", [str(claim["claim_id"])])
    entities = sorted(candidates.values(), key=lambda item: item["entity_id"])
    alias_records.sort(key=lambda item: item["alias_id"])
    return entities, alias_records


def _relations(claims: list[dict[str, Any]], entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {
        str(entity["canonical_name"]).casefold(): str(entity["entity_id"])
        for entity in entities
    }
    values: list[dict[str, Any]] = []
    for claim in claims:
        if claim.get("predicate") != "relation":
            continue
        relation = claim.get("object_or_value")
        if not isinstance(relation, dict):
            continue
        source_name = str(claim.get("subject") or "")
        target_name = str(relation.get("target") or "")
        source_id = by_name.get(source_name.casefold()) or stable_id(
            "ent", "character", source_name.casefold()
        )
        target_id = by_name.get(target_name.casefold()) or stable_id(
            "ent", "character", target_name.casefold()
        )
        values.append(
            {
                "relation_id": stable_id(
                    "rel", source_id, target_id, relation.get("type"), claim["claim_id"]
                ),
                "source_entity_id": source_id,
                "target_entity_id": target_id,
                "relation_type": relation.get("type") or "related",
                "dimensions": {
                    "trust": None,
                    "emotion": None,
                    "debt": None,
                    "authority": None,
                    "dependency": None,
                    "information_access": None,
                },
                "scope": claim.get("scope"),
                "knowledge_plane": claim.get("knowledge_plane"),
                "source_claim_id": claim["claim_id"],
            }
        )
    return sorted(values, key=lambda item: item["relation_id"])


def _world_structure(world_input: Any, claims: list[dict[str, Any]]) -> dict[str, Any]:
    world = copy.deepcopy(world_input) if isinstance(world_input, dict) else {}
    ontology = world.get("ontology") if isinstance(world.get("ontology"), dict) else {}
    ontology = {name: list(ontology.get(name) or []) for name in WORLD_OBJECT_TYPES}
    for claim in claims:
        predicate = str(claim.get("predicate") or "")
        record = {
            "claim_id": claim["claim_id"],
            "subject": claim["subject"],
            "value": copy.deepcopy(claim["object_or_value"]),
            "scope": claim.get("scope"),
            "knowledge_plane": claim.get("knowledge_plane"),
        }
        if predicate == "world.rule":
            ontology["Rule"].append(record)
        elif predicate in {"world.scarce_resource", "world.survival_resource"}:
            ontology["Stock"].append(record)
        elif predicate == "world.pressure":
            ontology["Pressure"].append(record)
        elif predicate == "actor.location" or predicate == "timeline.anchor":
            ontology["Coordinate"].append(record)
        elif predicate.startswith("actor."):
            ontology["Actor"].append(record)
        elif predicate == "relation":
            ontology["Relation"].append(record)
        elif claim.get("knowledge_plane") != "objective":
            ontology["Belief"].append(record)
        elif predicate.startswith("story.") or predicate == "open_loop":
            ontology["Event"].append(record)
        elif predicate.startswith("inventory."):
            ontology["Flow"].append(record)
    world["ontology"] = ontology

    modules = world.get("modules") if isinstance(world.get("modules"), dict) else {}
    normalized_modules: dict[str, Any] = {}
    for name in WORLD_MODULES:
        value = copy.deepcopy(modules.get(name))
        if value is None:
            value = {"status": "unknown", "elements": []}
        normalized_modules[name] = value
    world["modules"] = normalized_modules

    resolution = (
        world.get("resolution") if isinstance(world.get("resolution"), dict) else {}
    )
    normalized_resolution: dict[str, Any] = {}
    for level in RESOLUTION_LEVELS:
        existing = resolution.get(level)
        if isinstance(existing, dict):
            normalized_resolution[level] = copy.deepcopy(existing)
        else:
            normalized_resolution[level] = {
                "status": (
                    "defined"
                    if level == "kernel" and bool(world.get("rules") or world.get("mvw"))
                    else "deferred"
                ),
                "elements": [],
            }
    world["resolution"] = normalized_resolution

    mvw_input = world.get("mvw") if isinstance(world.get("mvw"), dict) else {}
    world["mvw"] = {key: copy.deepcopy(mvw_input.get(key)) for key in MVW_FIELDS}
    world["pressure_tests"] = _pressure_tests(world, claims)
    return world


def _meaningful_pressure_value(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        normalized = value.strip().casefold()
        return bool(normalized) and normalized not in {
            "unknown",
            "undefined",
            "tbd",
            "todo",
            "none",
            "null",
            "n/a",
            "未知",
            "未定义",
            "待定",
            "暂无",
        }
    if isinstance(value, Mapping):
        return any(_meaningful_pressure_value(child) for child in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_meaningful_pressure_value(child) for child in value)
    return True


def _distinct_pressure_entries(value: Any) -> int:
    if isinstance(value, (list, tuple, set, frozenset)):
        encoded = {
            canonical_json(child)
            for child in value
            if _meaningful_pressure_value(child)
        }
        return len(encoded)
    return 1 if _meaningful_pressure_value(value) else 0


def _pressure_chain_depth(value: Any) -> int:
    if isinstance(value, str):
        parts = re.split(r"\s*(?:→|->|=>|⇒|＞|>)\s*", value.strip())
        return len(
            {
                part.casefold()
                for part in parts
                if _meaningful_pressure_value(part)
            }
        )
    if isinstance(value, Mapping):
        return sum(
            _meaningful_pressure_value(child) for child in value.values()
        )
    return _distinct_pressure_entries(value)


def _pressure_value_preview(value: Any) -> Any:
    encoded = canonical_json(value)
    if len(encoded) <= 320:
        return copy.deepcopy(value)
    return {
        "truncated": True,
        "canonical_hash": canonical_hash(value),
        "preview": encoded[:256],
    }


def _pressure_evidence_observation(
    evidence_id: str,
    world: dict[str, Any],
    mvw: dict[str, Any],
) -> tuple[bool, str, Any]:
    if evidence_id == "daily_cycles":
        value = mvw.get("daily_cycles")
        count = _distinct_pressure_entries(value)
        return count >= 2, f"{count} distinct daily perspectives", value
    if evidence_id == "survival_resource_chain":
        value = mvw.get("survival_resource_chain")
        depth = _pressure_chain_depth(value)
        return depth >= 3, f"{depth} traceable supply stages", value
    if evidence_id == "power_actors":
        value = mvw.get("power_actors")
        count = _distinct_pressure_entries(value)
        return count >= 2, f"{count} distinct power actors", value
    if evidence_id == "pressure_horizons":
        value = mvw.get("pressure_horizons")
        required = ("near", "volume", "book")
        present = (
            [
                key
                for key in required
                if isinstance(value, Mapping)
                and _meaningful_pressure_value(value.get(key))
            ]
            if isinstance(value, Mapping)
            else []
        )
        return (
            len(present) == len(required),
            f"{len(present)}/{len(required)} pressure horizons defined",
            value,
        )
    if evidence_id == "formal_institution_chain":
        value = mvw.get("formal_institution_chain")
        depth = _pressure_chain_depth(value)
        return depth >= 3, f"{depth} traceable institution stages", value
    if evidence_id == "scarce_resources":
        value = world.get("scarce_resources")
        count = _distinct_pressure_entries(value)
        return count >= 1, f"{count} scarce resources identified", value
    if evidence_id == "infrastructure_bottleneck":
        value = mvw.get("infrastructure_bottleneck")
        sufficient = _meaningful_pressure_value(value)
        return sufficient, "bottleneck is defined" if sufficient else "bottleneck is absent", value
    if evidence_id == "harmed_group":
        value = mvw.get("harmed_group")
        sufficient = _meaningful_pressure_value(value)
        return sufficient, "affected group is defined" if sufficient else "affected group is absent", value
    if evidence_id == "core_capability":
        value = mvw.get("core_capability")
        required = ("capability", "cost", "boundary", "counter")
        present = (
            [
                key
                for key in required
                if isinstance(value, Mapping)
                and _meaningful_pressure_value(value.get(key))
            ]
            if isinstance(value, Mapping)
            else []
        )
        return (
            len(present) == len(required),
            f"{len(present)}/{len(required)} capability constraints defined",
            value,
        )
    if evidence_id == "base_rules":
        value = mvw.get("base_rules")
        count = _distinct_pressure_entries(value)
        return count >= 1, f"{count} minimum-world rules identified", value
    if evidence_id == "world_rules":
        value = world.get("rules")
        count = _distinct_pressure_entries(value)
        return count >= 1, f"{count} top-level world rules identified", value
    if evidence_id == "power_distribution":
        value = world.get("power_distribution")
        sufficient = _meaningful_pressure_value(value)
        return sufficient, "power distribution is defined" if sufficient else "power distribution is absent", value
    if evidence_id == "power_scarcity_chain":
        value = mvw.get("power_scarcity_chain")
        depth = _pressure_chain_depth(value)
        return depth >= 3, f"{depth} traceable scarcity-to-power stages", value
    if evidence_id == "important_secret":
        value = mvw.get("important_secret")
        sufficient = _meaningful_pressure_value(value)
        return sufficient, "protected information is defined" if sufficient else "protected information is absent", value
    if evidence_id == "legitimacy_narrative":
        value = mvw.get("legitimacy_narrative")
        sufficient = _meaningful_pressure_value(value)
        return sufficient, "public legitimacy narrative is defined" if sufficient else "public legitimacy narrative is absent", value
    if evidence_id == "story_clock":
        value = mvw.get("story_clock")
        sufficient = _meaningful_pressure_value(value)
        return sufficient, "story-time anchor is defined" if sufficient else "story-time anchor is absent", value
    if evidence_id == "locations_and_routes":
        value = mvw.get("locations_and_routes")
        count = _distinct_pressure_entries(value)
        return count >= 2, f"{count} distinct spatial anchors or routes", value
    if evidence_id == "historical_trauma":
        value = mvw.get("historical_trauma")
        sufficient = _meaningful_pressure_value(value)
        return sufficient, "historical causal residue is defined" if sufficient else "historical causal residue is absent", value
    if evidence_id == "current_pressures":
        value = world.get("current_pressures")
        count = _distinct_pressure_entries(value)
        return count >= 1, f"{count} active world pressures identified", value
    if evidence_id == "irreversible_trigger":
        value = mvw.get("irreversible_trigger")
        sufficient = _meaningful_pressure_value(value)
        return sufficient, "irreversible trigger is defined" if sufficient else "irreversible trigger is absent", value
    raise PlotInitError(
        "UNKNOWN_PRESSURE_EVIDENCE",
        f"unsupported world pressure evidence: {evidence_id}",
    )


def _pressure_tests(world: dict[str, Any], claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mvw = world.get("mvw") if isinstance(world.get("mvw"), dict) else {}
    evidence_definitions = {
        "daily_cycles": (
            "/world_model/mvw/daily_cycles",
            "at least two distinct daily perspectives",
        ),
        "survival_resource_chain": (
            "/world_model/mvw/survival_resource_chain",
            "a traceable supply chain with at least three stages",
        ),
        "power_actors": (
            "/world_model/mvw/power_actors",
            "at least two independently acting power centers",
        ),
        "pressure_horizons": (
            "/world_model/mvw/pressure_horizons",
            "near, volume, and book pressure horizons",
        ),
        "formal_institution_chain": (
            "/world_model/mvw/formal_institution_chain",
            "a traceable institutional response chain with at least three stages",
        ),
        "scarce_resources": (
            "/world_model/scarce_resources",
            "at least one named scarce resource",
        ),
        "infrastructure_bottleneck": (
            "/world_model/mvw/infrastructure_bottleneck",
            "a concrete infrastructure or capacity bottleneck",
        ),
        "harmed_group": (
            "/world_model/mvw/harmed_group",
            "a group that bears the tested system cost",
        ),
        "core_capability": (
            "/world_model/mvw/core_capability",
            "capability, cost, boundary, and counter are all defined",
        ),
        "base_rules": (
            "/world_model/mvw/base_rules",
            "at least one minimum-world invariant",
        ),
        "world_rules": (
            "/world_model/rules",
            "at least one top-level world invariant",
        ),
        "power_distribution": (
            "/world_model/power_distribution",
            "the current allocation of power is stated",
        ),
        "power_scarcity_chain": (
            "/world_model/mvw/power_scarcity_chain",
            "a traceable scarcity-to-power chain with at least three stages",
        ),
        "important_secret": (
            "/world_model/mvw/important_secret",
            "protected information capable of changing action or allocation",
        ),
        "legitimacy_narrative": (
            "/world_model/mvw/legitimacy_narrative",
            "the public justification for the current order",
        ),
        "story_clock": (
            "/world_model/mvw/story_clock",
            "a usable story-time anchor",
        ),
        "locations_and_routes": (
            "/world_model/mvw/locations_and_routes",
            "at least two distinct spatial anchors or routes",
        ),
        "historical_trauma": (
            "/world_model/mvw/historical_trauma",
            "a past event or debt with present causal residue",
        ),
        "current_pressures": (
            "/world_model/current_pressures",
            "at least one active world pressure",
        ),
        "irreversible_trigger": (
            "/world_model/mvw/irreversible_trigger",
            "a trigger that changes the starting equilibrium",
        ),
    }
    required_by_test = {
        "ordinary_day": ("daily_cycles", "survival_resource_chain"),
        "thirty_days_without_protagonist": (
            "power_actors",
            "pressure_horizons",
            "formal_institution_chain",
        ),
        "supply_cut": (
            "scarce_resources",
            "survival_resource_chain",
            "infrastructure_bottleneck",
            "harmed_group",
        ),
        "optimal_exploitation": (
            "world_rules",
            "base_rules",
            "core_capability",
        ),
        "power_vacuum": (
            "power_distribution",
            "power_actors",
            "formal_institution_chain",
            "power_scarcity_chain",
        ),
        "information_leak": (
            "important_secret",
            "legitimacy_narrative",
            "formal_institution_chain",
        ),
        "cross_class_view": ("daily_cycles", "harmed_group", "power_actors"),
        "spacetime_conservation": (
            "story_clock",
            "locations_and_routes",
            "base_rules",
        ),
        "historical_counterfactual": (
            "historical_trauma",
            "formal_institution_chain",
            "legitimacy_narrative",
        ),
        "plot_fertility": (
            "current_pressures",
            "pressure_horizons",
            "irreversible_trigger",
        ),
    }
    claim_predicates = {
        "world_rules": {"world.rule"},
        "base_rules": {"world.rule"},
        "scarce_resources": {
            "world.scarce_resource",
            "world.survival_resource",
        },
        "survival_resource_chain": {
            "world.scarce_resource",
            "world.survival_resource",
        },
        "power_scarcity_chain": {"world.scarce_resource"},
        "current_pressures": {"world.pressure"},
        "pressure_horizons": {"world.pressure"},
        "irreversible_trigger": {"story.inciting_event"},
    }
    results: list[dict[str, Any]] = []
    for test_id, label in PRESSURE_TESTS:
        evidence_ids = required_by_test[test_id]
        required_evidence: list[dict[str, Any]] = []
        observed_evidence: list[dict[str, Any]] = []
        satisfied_ids: list[str] = []
        test_claim_ids: set[str] = set()
        for evidence_id in evidence_ids:
            path, criterion = evidence_definitions[evidence_id]
            predicates = claim_predicates.get(evidence_id, set())
            source_claim_ids = sorted(
                {
                    str(claim["claim_id"])
                    for claim in claims
                    if claim.get("claim_id")
                    and str(claim.get("predicate") or "") in predicates
                }
            )
            test_claim_ids.update(source_claim_ids)
            satisfied, observation, value = _pressure_evidence_observation(
                evidence_id,
                world,
                mvw,
            )
            required_evidence.append(
                {
                    "evidence_id": evidence_id,
                    "path": path,
                    "criterion": criterion,
                }
            )
            observed_evidence.append(
                {
                    "evidence_id": evidence_id,
                    "path": path,
                    "satisfied": satisfied,
                    "observation": observation,
                    "value_preview": _pressure_value_preview(value),
                    "source_claim_ids": source_claim_ids[:16],
                }
            )
            if satisfied:
                satisfied_ids.append(evidence_id)
        missing_ids = [
            evidence_id
            for evidence_id in evidence_ids
            if evidence_id not in satisfied_ids
        ]
        if not missing_ids:
            status = "pass"
            reason = "all_required_evidence_satisfied"
            diagnostic = (
                f"{len(satisfied_ids)}/{len(evidence_ids)} required evidence "
                "checks passed"
            )
        elif not satisfied_ids:
            status = "fail"
            reason = f"missing_evidence:{','.join(missing_ids)}"
            diagnostic = (
                "no required evidence check passed; missing or structurally "
                f"insufficient: {', '.join(missing_ids)}"
            )
        else:
            status = "degraded"
            reason = f"missing_evidence:{','.join(missing_ids)}"
            diagnostic = (
                f"{len(satisfied_ids)}/{len(evidence_ids)} required evidence "
                "checks passed; missing or structurally insufficient: "
                f"{', '.join(missing_ids)}"
            )
        results.append(
            {
                "test_id": test_id,
                "label": label,
                "status": status,
                "required_evidence": required_evidence,
                "observed_evidence": observed_evidence,
                "diagnostic": diagnostic,
                "reason": reason,
                "evidence_fields": satisfied_ids,
                "source_claim_ids": sorted(test_claim_ids)[:16],
                "score": {
                    "satisfied": len(satisfied_ids),
                    "required": len(evidence_ids),
                    "ratio": round(
                        len(satisfied_ids) / len(evidence_ids),
                        6,
                    ),
                },
                "notes": (
                    "deterministic evidence-backed proposal-stage evaluation; "
                    "no facts inferred beyond the observed bundle fields"
                ),
            }
        )
    return results


def _source_ownership(
    field_states: dict[str, dict[str, Any]],
    source_manifest: list[dict[str, Any]],
) -> dict[str, str]:
    claim_to_source: dict[str, str] = {}
    for source in source_manifest:
        source_id = str(source.get("source_id") or "")
        claim_to_source[str(source.get("source_version_id") or "")] = source_id
    owners: dict[str, str] = {}
    for path, state in sorted(field_states.items()):
        refs = [str(value) for value in state.get("source_refs") or []]
        owner = ""
        for ref in refs:
            if ref.startswith("claim-"):
                owner = ref
                break
            if ref in claim_to_source:
                owner = claim_to_source[ref]
                break
        if not owner:
            if state.get("origin") == "user_input":
                owner = "seed:user"
            elif state.get("origin") == "model_suggestion":
                owner = "session:decision"
            else:
                owner = "session:derived"
        owners[path] = owner
    return owners


def _domain_plain_values(bundle: dict[str, Any]) -> bool:
    envelope_keys = {"field_status", "origin", "decision_status", "canon_status"}

    def inspect(value: Any) -> bool:
        if isinstance(value, dict):
            if envelope_keys.issubset(value):
                return False
            return all(inspect(child) for child in value.values())
        if isinstance(value, list):
            return all(inspect(child) for child in value)
        return True

    return all(inspect(bundle.get(key)) for key in DOMAIN_KEYS)


def _artifact_section(bundle: dict[str, Any], owner: str) -> Any:
    if owner == "power_overview":
        return {
            "schema_version": bundle.get("schema_version"),
            "power_systems": bundle.get("power_systems") or [],
            "power_model_status": (bundle.get("validation") or {}).get(
                "power_model_status"
            ),
        }
    if owner == "power_progression":
        return {
            "progression_tracks": bundle.get("progression_tracks") or [],
            "rank_nodes": bundle.get("rank_nodes") or [],
            "rank_edges": bundle.get("rank_edges") or [],
        }
    if owner == "power_resources":
        return {
            "resource_definitions": bundle.get("resource_definitions") or [],
            "rank_edges": bundle.get("rank_edges") or [],
        }
    if owner == "power_abilities":
        return {
            "ability_definitions": bundle.get("ability_definitions") or [],
            "actor_power_bootstrap": bundle.get("actor_power_bootstrap") or [],
        }
    if owner == "power_counters":
        return {
            "counter_rules": bundle.get("counter_rules") or [],
            "status_definitions": bundle.get("status_definitions") or [],
            "status_bootstrap": [
                {
                    "actor_id": item.get("actor_id"),
                    "statuses": item.get("statuses") or [],
                }
                for item in bundle.get("actor_power_bootstrap") or []
                if isinstance(item, dict) and item.get("statuses")
            ],
        }
    if owner == "power_bindings":
        return {
            "qualification_definitions": (
                bundle.get("qualification_definitions") or []
            ),
            "actor_bindings": [
                {
                    "actor_id": item.get("actor_id"),
                    "bindings": item.get("bindings") or [],
                    "qualifications": item.get("qualifications") or [],
                }
                for item in bundle.get("actor_power_bootstrap") or []
                if isinstance(item, dict)
            ]
        }
    if owner == "power_society":
        return {
            "systems": [
                {
                    "power_system_id": item.get("power_system_id"),
                    "social_consequences": item.get("social_consequences") or [],
                    "institutional_effects": item.get("institutional_effects") or [],
                }
                for item in bundle.get("power_systems") or []
                if isinstance(item, dict)
            ]
        }
    if owner == "power_bridges":
        return {
            "bridge_rules": bundle.get("bridge_rules") or [],
            "conversion_rules": bundle.get("conversion_rules") or [],
            "native_term_bindings": [
                binding
                for item in bundle.get("power_systems") or []
                if isinstance(item, dict)
                for binding in item.get("native_term_bindings") or []
            ],
        }
    if owner == "world_spacetime":
        world = bundle["world_model"]
        return {
            "resolution": world.get("resolution"),
            "coordinates": world.get("ontology", {}).get("Coordinate"),
            "story_clock": world.get("mvw", {}).get("story_clock"),
            "locations_and_routes": world.get("mvw", {}).get("locations_and_routes"),
        }
    if owner == "world_rules":
        world = bundle["world_model"]
        return {
            "rules": world.get("rules"),
            "ontology_rules": world.get("ontology", {}).get("Rule"),
            "core_capability": world.get("mvw", {}).get("core_capability"),
        }
    if owner == "world_resources":
        world = bundle["world_model"]
        return {
            "scarce_resources": world.get("scarce_resources"),
            "survival_resource_chain": world.get("mvw", {}).get(
                "survival_resource_chain"
            ),
            "power_scarcity_chain": world.get("mvw", {}).get(
                "power_scarcity_chain"
            ),
            "daily_cycles": world.get("mvw", {}).get("daily_cycles"),
            "modules": world.get("modules"),
        }
    if owner == "world_pressure":
        world = bundle["world_model"]
        return {
            "historical_trauma": world.get("mvw", {}).get("historical_trauma"),
            "pressure_horizons": world.get("mvw", {}).get("pressure_horizons"),
            "pressure_tests": world.get("pressure_tests"),
        }
    if owner == "story_outline":
        return {
            "first_event_chain": bundle["story_engine"].get("first_event_chain"),
            "volume_one_change": bundle["story_engine"].get("volume_one_change"),
            "endgame_direction": bundle["story_engine"].get("endgame_direction"),
        }
    if owner == "project_config":
        item_sidecars = (
            ((bundle.get("provenance") or {}).get("item_sidecars") or [])
            if isinstance(bundle.get("provenance"), dict)
            else []
        )
        item_sidecar = (
            copy.deepcopy(item_sidecars[0])
            if len(item_sidecars) == 1 and isinstance(item_sidecars[0], dict)
            else None
        )
        advantage_sidecars = (
            ((bundle.get("provenance") or {}).get("advantage_sidecars") or [])
            if isinstance(bundle.get("provenance"), dict)
            else []
        )
        advantage_sidecar = (
            copy.deepcopy(advantage_sidecars[0])
            if len(advantage_sidecars) == 1
            and isinstance(advantage_sidecars[0], dict)
            else None
        )
        return {
            "config_version": 3,
            "enabled": True,
            "grill": {
                "enabled": True,
                "schema_version": "plot-rag-intent/v1",
                "database_path": ".plot-rag/grill.sqlite3",
                "one_question_per_turn": True,
                "recommend_answer": True,
                "explore_project_first": True,
                "max_questions": 6,
                "session_ttl_seconds": 21600,
                "required_fields": [
                    "problem_to_solve",
                    "expected_deliverable",
                    "reader_experience",
                    "protagonist_drive_conflict",
                    "scope_endpoint",
                    "success_criteria",
                    "hard_constraints",
                    "model_autonomy",
                ],
                "skip_phrases": [
                    "跳过 Grill",
                    "跳过盘问",
                    "跳过目的确认",
                    "按现有要求直接执行",
                    "直接执行，不要追问",
                ],
                "cancel_phrases": [
                    "取消本轮 Grill",
                    "结束本轮盘问",
                    "停止本轮盘问",
                    "放弃本轮任务",
                ],
            },
            "authority_sources": [
                {
                    "glob": "正文/**/*.md",
                    "role": "canon",
                    "scope_policy": "infer_and_review",
                    "ingest_policy": "include",
                    "priority": 100,
                },
                {
                    "glob": "设定集/**/*.md",
                    "role": "setting",
                    "scope_policy": "timeless_candidate",
                    "ingest_policy": "include",
                    "priority": 90,
                },
                {
                    "glob": "剧情/**/*.md",
                    "role": "outline",
                    "scope_policy": "planned_only",
                    "ingest_policy": "review",
                    "priority": 60,
                },
            ],
            "initialization": {
                "schema_version": str(
                    bundle.get("schema_version") or PROTOCOL_V1
                ),
                "database_path": ".plot-rag/init.sqlite3",
                "proposal_only": True,
            },
            **(
                {
                    "items": {
                        "schema_version": item_sidecar["schema_version"],
                        "sidecar_path": item_sidecar["path"],
                        "package_hash": item_sidecar["package_hash"],
                        "content_hash": item_sidecar["content_hash"],
                    }
                }
                if item_sidecar is not None
                else {}
            ),
            **(
                {
                    "advantage": {
                        "enabled": False,
                        "shadow": True,
                        "schema_version": advantage_sidecar[
                            "schema_version"
                        ],
                        "strict_runtime_validation": False,
                        "readable_projection": True,
                        "mandatory_context": True,
                        "sidecar_path": advantage_sidecar["path"],
                        "package_hash": advantage_sidecar["package_hash"],
                        "content_hash": advantage_sidecar["content_hash"],
                    }
                }
                if advantage_sidecar is not None
                else {}
            ),
            "power_system": {
                "mode": "auto",
                "schema_version": "plot-rag-power/v1",
                "strict_progression": True,
                "comparison_mode": "conditional",
                "unknown_policy": "quarantine",
                "profiles": [
                    str(item.get("profile"))
                    for item in bundle.get("power_systems") or []
                    if isinstance(item, dict) and item.get("profile")
                ],
            },
        }
    return bundle.get(owner)


def _render_artifact(title: str, payload: Any, *, as_json: bool = False) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if as_json:
        return encoded + "\n"
    return f"# {title}\n\n```json\n{encoded}\n```\n"


def _merge_project_config(
    existing: dict[str, Any],
    generated: dict[str, Any],
) -> dict[str, Any]:
    """Upgrade required initialization fields without erasing user config."""

    merged = copy.deepcopy(existing)
    merged["config_version"] = 3
    merged.setdefault("enabled", generated.get("enabled", True))
    current_grill = merged.get("grill")
    if not isinstance(current_grill, dict):
        current_grill = {}
    grill_defaults = dict(generated.get("grill") or {})
    for key, value in grill_defaults.items():
        current_grill.setdefault(key, copy.deepcopy(value))
    merged["grill"] = current_grill

    existing_sources = merged.get("authority_sources")
    if not isinstance(existing_sources, list) or not existing_sources:
        merged["authority_sources"] = copy.deepcopy(
            generated.get("authority_sources") or []
        )
    else:
        combined_sources = copy.deepcopy(existing_sources)
        seen_globs = {
            str(item.get("glob") or "").replace("\\", "/").casefold()
            for item in combined_sources
            if isinstance(item, Mapping) and str(item.get("glob") or "").strip()
        }
        for generated_source in generated.get("authority_sources") or []:
            if not isinstance(generated_source, Mapping):
                continue
            normalized_glob = (
                str(generated_source.get("glob") or "")
                .replace("\\", "/")
                .casefold()
            )
            if not normalized_glob or normalized_glob in seen_globs:
                continue
            combined_sources.append(copy.deepcopy(dict(generated_source)))
            seen_globs.add(normalized_glob)
        merged["authority_sources"] = combined_sources

    current_initialization = merged.get("initialization")
    if not isinstance(current_initialization, dict):
        current_initialization = {}
    initialization_defaults = dict(generated.get("initialization") or {})
    for key, value in initialization_defaults.items():
        current_initialization.setdefault(key, copy.deepcopy(value))
    # The accepted initialization runtime must remain proposal-only even when
    # an older local config carried an unsafe or stale value.
    current_initialization["schema_version"] = str(
        initialization_defaults.get("schema_version") or PROTOCOL_V1
    )
    current_initialization["proposal_only"] = True
    merged["initialization"] = current_initialization
    generated_items = generated.get("items")
    if isinstance(generated_items, dict):
        current_items = merged.get("items")
        if not isinstance(current_items, dict):
            current_items = {}
        for key, value in generated_items.items():
            current_items[key] = copy.deepcopy(value)
        merged["items"] = current_items
    generated_advantage = generated.get("advantage")
    if isinstance(generated_advantage, dict):
        current_advantage = merged.get("advantage")
        if not isinstance(current_advantage, dict):
            current_advantage = {}
        identity_fields = {
            "schema_version",
            "sidecar_path",
            "package_hash",
            "content_hash",
        }
        for key, value in generated_advantage.items():
            if key in identity_fields:
                current_advantage[key] = copy.deepcopy(value)
            else:
                current_advantage.setdefault(key, copy.deepcopy(value))
        merged["advantage"] = current_advantage
    return merged


def _artifact_manifest(
    bundle: dict[str, Any],
    project_root: Path | None,
    *,
    item_artifact: Mapping[str, Any] | None = None,
    advantage_artifact: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    artifact_specs = list(STANDARD_ARTIFACTS)
    if bundle.get("schema_version") == PROTOCOL_V2:
        artifact_specs.extend(POWER_ARTIFACTS)
    for relative, owner, title in artifact_specs:
        payload = _artifact_section(bundle, owner)
        existing = ""
        existing_raw: bytes | None = None
        expected_old_hash: str | None = None
        target_exists = False
        if project_root is not None:
            target = (project_root / Path(relative)).resolve(strict=False)
            if not path_is_within(target, project_root):
                raise PlotInitError(
                    "UNSAFE_TARGET_PATH",
                    f"artifact target escapes project root: {relative}",
                )
            if target.is_file():
                raw = target.read_bytes()
                existing_raw = raw
                target_exists = True
                expected_old_hash = sha256_bytes(raw)
                try:
                    existing = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    existing = ""
        if owner == "project_config" and existing_raw is not None:
            try:
                parsed_existing = json.loads(existing_raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PlotInitError(
                    "INVALID_EXISTING_PROJECT_CONFIG",
                    "existing .plot-rag/config.json must be valid UTF-8 JSON before initialization",
                    path=relative,
                ) from exc
            if not isinstance(parsed_existing, dict):
                raise PlotInitError(
                    "INVALID_EXISTING_PROJECT_CONFIG",
                    "existing .plot-rag/config.json root must be an object",
                    path=relative,
                )
            payload = _merge_project_config(
                parsed_existing,
                payload if isinstance(payload, dict) else {},
            )
        content = _render_artifact(
            title,
            payload,
            as_json=relative.casefold().endswith(".json"),
        )
        proposed_hash = sha256_bytes(content.encode("utf-8"))
        operation = (
            "create"
            if not target_exists
            else "noop"
            if expected_old_hash == proposed_hash
            else "update"
        )
        diff = ""
        if operation != "noop":
            diff = "".join(
                difflib.unified_diff(
                    existing.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
        values.append(
            {
                "artifact_id": stable_id("artifact", relative, proposed_hash),
                "path": relative,
                "logical_owner": owner,
                "operation": operation,
                "expected_old_hash": expected_old_hash,
                "proposed_new_hash": proposed_hash,
                "proposed_content": content,
                "unified_diff": diff,
                "materialized": False,
            }
        )
    if item_artifact is not None:
        artifact = copy.deepcopy(dict(item_artifact))
        reference = item_sidecar_reference(artifact)
        if any(
            str(existing.get("path") or "") == reference["path"]
            or str(existing.get("artifact_id") or "") == reference["artifact_id"]
            for existing in values
        ):
            raise PlotInitError(
                "ITEM_SIDECAR_DUPLICATE",
                "item sidecar conflicts with a standard initialization artifact",
                path=reference["path"],
                artifact_id=reference["artifact_id"],
            )
        values.append(artifact)
    if advantage_artifact is not None:
        artifact = copy.deepcopy(dict(advantage_artifact))
        reference = advantage_sidecar_reference(artifact)
        if any(
            str(existing.get("path") or "") == reference["path"]
            or str(existing.get("artifact_id") or "")
            == reference["artifact_id"]
            for existing in values
        ):
            raise PlotInitError(
                "ADVANTAGE_SIDECAR_DUPLICATE",
                "Advantage sidecar conflicts with another initialization artifact",
                path=reference["path"],
                artifact_id=reference["artifact_id"],
            )
        values.append(artifact)
    return values


def _roundtrip_export_record(state: dict[str, Any]) -> dict[str, Any] | None:
    """Return an isolated normalized import eligible for lossless replay."""

    if state.get("mode") != "ingest":
        return None
    if state.get("seed_dossier") or state.get("answer_patches"):
        return None
    effective_source_ids = [
        str(item.get("source_id") or "")
        for item in state.get("source_manifest") or []
        if item.get("parse_status") == "parsed"
        and item.get("ingest_policy") != "exclude"
    ]
    if len(effective_source_ids) != 1:
        return None
    record = (state.get("normalized_exports") or {}).get(effective_source_ids[0])
    if not isinstance(record, dict) or not isinstance(record.get("bundle"), dict):
        return None
    return record


def _build_roundtrip_bundle(
    state: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    """Rebind operational metadata while retaining all semantic envelopes."""

    original = copy.deepcopy(record["bundle"])
    bundle = copy.deepcopy(original)
    meta = bundle.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        bundle["meta"] = meta
    meta.update(
        {
            "protocol": str(bundle.get("schema_version") or PROTOCOL_V1),
            "bundle_schema_version": str(
                bundle.get("schema_version") or PROTOCOL_V1
            ),
            "session_id": state["session_id"],
            "session_revision": state["session_revision"],
            "expected_canon_revision": state["expected_canon_revision"],
            "proposal_only": True,
        }
    )
    project_root = (
        Path(state["project_root"]).resolve(strict=False)
        if state.get("project_root")
        else None
    )
    loaded_item_sidecar = item_package_from_artifact_manifest(
        item
        for item in original.get("artifact_manifest") or []
        if isinstance(item, Mapping)
    )
    loaded_advantage_sidecar = advantage_package_from_artifact_manifest(
        item
        for item in original.get("artifact_manifest") or []
        if isinstance(item, Mapping)
    )
    item_artifact: dict[str, Any] | None = None
    if loaded_item_sidecar is not None:
        item_package, _original_reference = loaded_item_sidecar
        item_artifact = build_item_sidecar_artifact(
            item_package,
            project_root,
        )
        provenance = bundle.get("provenance")
        if not isinstance(provenance, dict):
            provenance = {}
            bundle["provenance"] = provenance
        provenance["item_sidecars"] = [
            item_sidecar_reference(item_artifact)
        ]
    advantage_artifact: dict[str, Any] | None = None
    if loaded_advantage_sidecar is not None:
        advantage_package, _original_reference = loaded_advantage_sidecar
        advantage_artifact = build_advantage_sidecar_artifact(
            advantage_package,
            project_root,
        )
        provenance = bundle.get("provenance")
        if not isinstance(provenance, dict):
            provenance = {}
            bundle["provenance"] = provenance
        provenance["advantage_sidecars"] = [
            advantage_sidecar_reference(advantage_artifact)
        ]
    bundle["artifact_manifest"] = _artifact_manifest(
        bundle,
        project_root,
        item_artifact=item_artifact,
        advantage_artifact=advantage_artifact,
    )
    bundle_hash = recompute_bundle_hash(bundle)
    bundle["bundle_hash"] = bundle_hash

    semantic_diff = normalization_diff(original, bundle)
    semantic_hash = normalized_hash(bundle)
    state["field_states"] = copy.deepcopy(bundle.get("field_states") or {})
    state["conflicts"] = copy.deepcopy(bundle.get("conflicts") or [])
    state["gaps"] = copy.deepcopy(bundle.get("gaps") or [])
    state["normalization_roundtrip"] = {
        "format": "plot-rag-init-normalized/v1",
        "source_bundle_hash": str(original.get("bundle_hash") or ""),
        "result_bundle_hash": bundle_hash,
        "source_normalization_hash": str(record.get("normalization_hash") or ""),
        "result_normalization_hash": semantic_hash,
        "stable_hash": (
            str(record.get("normalization_hash") or "") == semantic_hash
        ),
        "bundle_hash_stable": (
            str(original.get("bundle_hash") or "") == bundle_hash
        ),
        "diff": semantic_diff,
        "zero_diff": not semantic_diff,
        "transport_source_hash": record.get("transport_source_hash"),
    }
    state["bundle"] = bundle
    state["bundle_hash"] = bundle_hash
    return bundle


def build_bundle(state: dict[str, Any]) -> dict[str, Any]:
    roundtrip_record = _roundtrip_export_record(state)
    if roundtrip_record is not None:
        return _build_roundtrip_bundle(state, roundtrip_record)

    dossier = copy.deepcopy(state.get("dossier") or {})
    claims = _claims(state)
    bundle_schema_version = str(
        state.get("bundle_schema_version") or PROTOCOL_V1
    )
    power_package = (
        build_power_package(
            dossier,
            claims,
            mode=str(state.get("mode") or "new"),
        )
        if bundle_schema_version == PROTOCOL_V2
        else None
    )
    entities, aliases = _entities_and_aliases(claims, dossier)
    dossier_entities = dossier.get("entities")
    if isinstance(dossier_entities, list):
        existing_ids = {item["entity_id"] for item in entities}
        for item in dossier_entities:
            candidate = copy.deepcopy(item)
            if not isinstance(candidate, dict):
                continue
            candidate.setdefault(
                "entity_id",
                stable_id(
                    "ent",
                    candidate.get("entity_type") or "concept",
                    str(candidate.get("canonical_name") or candidate.get("name") or "").casefold(),
                ),
            )
            if candidate["entity_id"] not in existing_ids:
                entities.append(candidate)
                existing_ids.add(candidate["entity_id"])
    entities.sort(key=lambda item: str(item.get("entity_id")))
    relations = _relations(claims, entities)
    if isinstance(dossier.get("relations"), list):
        relations = _deep_merge(relations, dossier["relations"])
    timeline = (
        copy.deepcopy(dossier.get("timeline"))
        if isinstance(dossier.get("timeline"), list)
        else []
    )
    for claim in claims:
        if claim.get("predicate") == "timeline.anchor":
            record = {
                "timeline_id": stable_id("time", claim["claim_id"]),
                "label": claim.get("subject"),
                "story_time": claim.get("story_time") or claim.get("object_or_value"),
                "scope": claim.get("scope"),
                "branch_id": claim.get("branch_id"),
                "knowledge_plane": claim.get("knowledge_plane"),
                "source_claim_id": claim["claim_id"],
            }
            if canonical_json(record) not in {canonical_json(item) for item in timeline}:
                timeline.append(record)
    open_loops = (
        copy.deepcopy(dossier.get("open_loops"))
        if isinstance(dossier.get("open_loops"), list)
        else []
    )
    field_states = copy.deepcopy(state.get("field_states") or {})
    world = _world_structure(dossier.get("world_model"), claims)
    domain_payload = {
        "genre_contract": copy.deepcopy(dossier.get("genre_contract") or {}),
        "world_model": world,
        "actor_system": copy.deepcopy(dossier.get("actor_system") or {}),
        "story_engine": copy.deepcopy(dossier.get("story_engine") or {}),
        "serialization_contract": copy.deepcopy(
            dossier.get("serialization_contract") or {}
        ),
        "entities": entities,
        "relations": relations,
        "timeline": timeline,
        "open_loops": open_loops,
    }
    if power_package is not None:
        for key in POWER_DOMAIN_KEYS:
            domain_payload[key] = copy.deepcopy(power_package[key])
    for path, _ in _walk_leaves(domain_payload):
        field_states.setdefault(
            path,
            _field_state(
                field_status="unknown",
                origin="deterministic_derived",
                decision_status="open",
                confidence=0.0,
            ),
        )
    state["field_states"] = field_states
    ownership = _source_ownership(field_states, state.get("source_manifest") or [])
    conflicts = _conflicts(claims)
    state["conflicts"] = conflicts
    gaps = _gaps(state)
    state["gaps"] = gaps
    source_manifest = copy.deepcopy(state.get("source_manifest") or [])
    remote_reviews: list[dict[str, Any]] = []
    review_fields = (
        "protocol",
        "status",
        "error_code",
        "model",
        "cache_hit",
        "accepted_count",
        "rejected_count",
        "response_hash",
    )
    for source in sorted(
        source_manifest,
        key=lambda item: str(item.get("source_id") or ""),
    ):
        for stage, field in (
            ("classification", "remote_classification_review"),
            ("claims", "remote_claim_review"),
        ):
            review = source.get(field)
            if not isinstance(review, dict):
                continue
            remote_reviews.append(
                {
                    "source_id": str(source.get("source_id") or ""),
                    "source_version_id": str(
                        source.get("source_version_id") or ""
                    ),
                    "stage": stage,
                    **{
                        key: copy.deepcopy(review.get(key))
                        for key in review_fields
                    },
                }
            )
    remote_model_used = any(
        review.get("status") == "accepted"
        and int(review.get("accepted_count") or 0) > 0
        for review in remote_reviews
    )
    remote_review_summary = {
        "review_count": len(remote_reviews),
        "accepted_count": sum(
            int(review.get("accepted_count") or 0)
            for review in remote_reviews
        ),
        "rejected_count": sum(
            int(review.get("rejected_count") or 0)
            for review in remote_reviews
        ),
        "cache_hit_count": sum(
            bool(review.get("cache_hit")) for review in remote_reviews
        ),
        "models": sorted(
            {
                str(review.get("model"))
                for review in remote_reviews
                if review.get("model")
            }
        ),
        "response_hashes": sorted(
            {
                str(review.get("response_hash"))
                for review in remote_reviews
                if review.get("response_hash")
            }
        ),
        "reviews": remote_reviews,
    }
    proposal_readiness = not any(gap["blocks_proposal"] for gap in gaps)
    if state["mode"] == "ingest":
        proposal_readiness = True
    work_id = stable_id(
        "work",
        state.get("project_root") or state.get("workspace_root"),
        (domain_payload["genre_contract"] or {}).get("primary_engine"),
    )
    item_package = build_item_package(
        dossier,
        claims,
        work_id=work_id,
        source_initialization_schema_version=bundle_schema_version,
        source_snapshot_hash=str(state.get("source_snapshot_hash") or ""),
    )
    project_root = (
        Path(state["project_root"]).resolve(strict=False)
        if state.get("project_root")
        else None
    )
    item_artifact = (
        build_item_sidecar_artifact(item_package, project_root)
        if item_package_has_typed_content(item_package)
        else None
    )
    item_sidecars = (
        [item_sidecar_reference(item_artifact)]
        if item_artifact is not None
        else []
    )
    advantage_package = build_advantage_package(
        dossier,
        claims,
        work_id=work_id,
        source_initialization_schema_version=bundle_schema_version,
        source_snapshot_hash=str(state.get("source_snapshot_hash") or ""),
    )
    advantage_artifact = (
        build_advantage_sidecar_artifact(advantage_package, project_root)
        if advantage_package_has_typed_content(advantage_package)
        else None
    )
    advantage_sidecars = (
        [advantage_sidecar_reference(advantage_artifact)]
        if advantage_artifact is not None
        else []
    )
    bundle: dict[str, Any] = {
        "schema_version": bundle_schema_version,
        "meta": {
            "protocol": bundle_schema_version,
            "bundle_schema_version": bundle_schema_version,
            "power_schema_version": (
                power_package.get("schema_version")
                if power_package is not None
                else None
            ),
            "work_id": work_id,
            "mode": state["mode"],
            "target_profile": state["target_profile"],
            "interaction_profile": state["interaction_profile"],
            "session_id": state["session_id"],
            "session_revision": state["session_revision"],
            "expected_canon_revision": state["expected_canon_revision"],
            "source_snapshot_hash": state["source_snapshot_hash"],
            "proposal_only": True,
        },
        **domain_payload,
        "field_states": field_states,
        "source_manifest": source_manifest,
        "source_ownership": ownership,
        "conflicts": conflicts,
        "gaps": gaps,
        "decisions": copy.deepcopy(state.get("decisions") or []),
        "provenance": {
            "claims": claims,
            "entity_aliases": aliases,
            "source_issues": copy.deepcopy(state.get("source_issues") or []),
            "duplicate_sources": copy.deepcopy(state.get("duplicate_sources") or []),
            "answer_refs": [
                {
                    "question_id": question_id,
                    "answer_hash": canonical_hash(answer),
                }
                for question_id, answer in sorted((state.get("answers") or {}).items())
            ],
            "extractor": (
                "local-deterministic-v1+remote-ambiguity-review-v1"
                if remote_model_used
                else "local-deterministic-v1"
            ),
            "remote_model_used": remote_model_used,
            "remote_review": remote_review_summary,
            "legacy_power_payload": (
                {
                    key: copy.deepcopy(dossier.get(key))
                    for key in (
                        "power_profile",
                        "power_system",
                        *POWER_DOMAIN_KEYS,
                    )
                    if dossier.get(key) not in (None, "", [], {})
                }
                if power_package is None
                else {}
            ),
            **(
                {"item_sidecars": item_sidecars}
                if item_sidecars
                else {}
            ),
            **(
                {"advantage_sidecars": advantage_sidecars}
                if advantage_sidecars
                else {}
            ),
        },
        "artifact_manifest": [],
        "validation": {
            "schema_valid": True,
            "domain_plain_values": True,
            "unique_source_ownership": len(ownership) == len(set(ownership.keys())),
            "source_reference_coverage": (
                1.0
                if not claims
                else sum(bool(claim.get("exact_evidence")) for claim in claims)
                / len(claims)
            ),
            "proposal_ready": proposal_readiness,
            "preapproval_canon_delta": 0,
            "pressure_tests": {
                item["test_id"]: item["status"]
                for item in world["pressure_tests"]
            },
            "hard_gap_count": sum(
                gap["severity"] == "hard" for gap in gaps
            ),
            "blocking_gap_count": sum(gap["blocks_proposal"] for gap in gaps),
            "power_model_status": (
                power_package.get("power_model_status")
                if power_package is not None
                else "unmodeled"
            ),
            "power_sufficiency": (
                power_sufficiency(
                    power_package,
                    mode=str(state.get("mode") or "new"),
                )
                if power_package is not None
                else {
                    "sufficient": True,
                    "profile": None,
                    "power_model_status": "unmodeled",
                    "checks": {},
                    "blocking_checks": [],
                    "compatibility": "v1_fallback",
                }
            ),
        },
    }
    if power_package is not None:
        bundle["power_model"] = copy.deepcopy(power_package)
    bundle["artifact_manifest"] = _artifact_manifest(
        bundle,
        project_root,
        item_artifact=item_artifact,
        advantage_artifact=advantage_artifact,
    )
    bundle["validation"]["domain_plain_values"] = _domain_plain_values(bundle)
    if not bundle["validation"]["domain_plain_values"]:
        raise PlotInitError(
            "DOMAIN_ENVELOPE_DETECTED",
            "domain values must remain plain; metadata belongs only in field_states",
        )
    bundle["validation"]["normalization_hash"] = normalized_hash(bundle)
    bundle_hash = recompute_bundle_hash(bundle)
    bundle["bundle_hash"] = bundle_hash
    state["bundle"] = bundle
    state["bundle_hash"] = bundle_hash
    return bundle


def _checkpoint(
    state: dict[str, Any],
    checkpoints: list[dict[str, Any]],
    stage: str,
    *,
    status: str = "ACTIVE",
    reason: str = "",
) -> None:
    state["stage"] = stage
    state["status"] = status
    checkpoints.append(
        {
            "checkpoint_id": stable_id(
                "checkpoint",
                state["session_id"],
                state["session_revision"],
                len(checkpoints),
                stage,
                state.get("source_snapshot_hash"),
                canonical_hash(state.get("answers") or {}),
            ),
            "stage": stage,
            "status": status,
            "reason": reason,
            "source_snapshot_hash": state.get("source_snapshot_hash"),
            "dependency_hash": canonical_hash(
                {
                    "answers": state.get("answers") or {},
                    "sources": state.get("source_snapshot_hash"),
                    "invalidated": state.get("invalidated_nodes") or [],
                }
            ),
        }
    )


def drive_state(
    state: dict[str, Any],
    *,
    refresh_inventory: bool = True,
    remote_cache: "RemoteResponseCache | None" = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run deterministic stages until a user decision or proposal boundary."""

    state = copy.deepcopy(state)
    state["remote_cache_binding"] = (
        remote_cache.describe() if remote_cache is not None else None
    )
    if state.get("status") in {"PROPOSAL_FROZEN", "CANCELLED"}:
        return state, []
    checkpoints: list[dict[str, Any]] = []
    state["current_questions"] = []
    state["status"] = "ACTIVE"
    _checkpoint(state, checkpoints, "DISCOVER")

    requested = str(state.get("requested_mode") or "auto")
    should_inventory = requested in {"auto", "ingest", "hybrid"} and bool(
        state.get("source_paths")
    )
    if should_inventory and refresh_inventory:
        refresh_sources(state, remote_cache=remote_cache)
    _checkpoint(state, checkpoints, "ROUTING")
    _route_mode(state)

    if state["mode"] in {"ingest", "hybrid"}:
        _checkpoint(state, checkpoints, "INVENTORY")
        _checkpoint(state, checkpoints, "CLASSIFY")
        _checkpoint(state, checkpoints, "EXTRACT")
        claims = _claims(state)
        state["conflicts"] = _conflicts(claims)
        _checkpoint(state, checkpoints, "CONFLICT")
        _compose_dossier(state)
        state["gaps"] = _gaps(state)
        _checkpoint(state, checkpoints, "GAP")
        if state["mode"] == "ingest":
            _checkpoint(state, checkpoints, "NORMALIZE")
            build_bundle(state)
            _checkpoint(state, checkpoints, "VALIDATE")
            _checkpoint(state, checkpoints, "REVIEW")
            _checkpoint(
                state,
                checkpoints,
                "READY_TO_PROPOSE",
                status="READY_TO_PROPOSE",
            )
            state["checkpoints"] = checkpoints
            state["updated_at"] = utc_now()
            return state, checkpoints

    _compose_dossier(state)
    if not _genre_sufficient(state["dossier"]):
        _need_question(state, "genre-contract")
        _checkpoint(
            state,
            checkpoints,
            "GENRE_CONTRACT",
            status="NEEDS_INPUT",
            reason="genre contract decision package required",
        )
        state["checkpoints"] = checkpoints
        state["updated_at"] = utc_now()
        return state, checkpoints
    _checkpoint(state, checkpoints, "GENRE_CONTRACT")

    if not _world_sufficient(state["dossier"]):
        _need_question(state, "world-causal-kernel")
        _checkpoint(
            state,
            checkpoints,
            "WORLD_CAUSAL_KERNEL",
            status="NEEDS_INPUT",
            reason="world causal kernel decision package required",
        )
        state["checkpoints"] = checkpoints
        state["updated_at"] = utc_now()
        return state, checkpoints
    _checkpoint(state, checkpoints, "WORLD_CAUSAL_KERNEL")

    if state.get("bundle_schema_version") == PROTOCOL_V2:
        power_report = _power_report(state)["sufficiency"]
        if not power_report["sufficient"]:
            _need_question(state, "power-causal-kernel")
            _checkpoint(
                state,
                checkpoints,
                "POWER_CAUSAL_KERNEL",
                status="NEEDS_INPUT",
                reason="power causal kernel decision package required",
            )
            state["checkpoints"] = checkpoints
            state["updated_at"] = utc_now()
            return state, checkpoints
        _checkpoint(state, checkpoints, "POWER_CAUSAL_KERNEL")

    if not _actor_sufficient(state["dossier"]) or not _story_sufficient(
        state["dossier"]
    ):
        _need_question(state, "story-engine")
        _checkpoint(
            state,
            checkpoints,
            "STORY_ENGINE",
            status="NEEDS_INPUT",
            reason="actor anchor and story engine decision package required",
        )
        state["checkpoints"] = checkpoints
        state["updated_at"] = utc_now()
        return state, checkpoints
    _checkpoint(state, checkpoints, "ACTOR_ANCHOR")
    _checkpoint(state, checkpoints, "STORY_ENGINE")
    _derive_serialization(state)
    _checkpoint(state, checkpoints, "SERIALIZATION_CONTRACT")

    _checkpoint(state, checkpoints, "NORMALIZE")
    build_bundle(state)
    _checkpoint(state, checkpoints, "VALIDATE")
    if not state["bundle"]["validation"]["proposal_ready"]:
        state["status"] = "NEEDS_INPUT"
        state["stage"] = "REVIEW"
        _checkpoint(
            state,
            checkpoints,
            "REVIEW",
            status="NEEDS_INPUT",
            reason="blocking gaps remain after normalization",
        )
        state["checkpoints"] = checkpoints
        state["updated_at"] = utc_now()
        return state, checkpoints
    _checkpoint(state, checkpoints, "REVIEW")
    _checkpoint(
        state,
        checkpoints,
        "READY_TO_PROPOSE",
        status="READY_TO_PROPOSE",
    )
    state["checkpoints"] = checkpoints
    state["updated_at"] = utc_now()
    return state, checkpoints


def _natural_answer_patch(question_id: str, text: str, state: dict[str, Any]) -> dict[str, Any]:
    cleaned = text.strip()
    if question_id == "genre-contract":
        option = _genre_options(state)[0]["patch"]
        patch = copy.deepcopy(option)
        patch["genre_contract"]["primary_engine"] = cleaned
        patch["genre_contract"]["differentiators"] = [cleaned]
        return patch
    if question_id == "world-causal-kernel":
        option = _world_options(state)[0]["patch"]
        patch = copy.deepcopy(option)
        patch["world_model"]["rules"] = [cleaned]
        patch["world_model"]["mvw"]["base_rules"] = [cleaned]
        return patch
    if question_id == "power-causal-kernel":
        option = _power_options(state)[0]["patch"]
        patch = copy.deepcopy(option)
        if cleaned:
            patch["power_systems"][0]["name"] = cleaned
        return patch
    option = _story_options(state)[0]["patch"]
    patch = copy.deepcopy(option)
    patch["story_engine"]["inciting_event"] = cleaned
    patch["story_engine"]["first_event_chain"][0] = cleaned
    return patch


def apply_answers(
    state: dict[str, Any],
    answers: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    state = copy.deepcopy(state)
    if state.get("status") == "PROPOSAL_FROZEN":
        raise PlotInitError(
            "PROPOSAL_IMMUTABLE",
            "a frozen initialization proposal cannot be changed",
        )
    known_questions = {
        "genre-contract",
        "world-causal-kernel",
        "power-causal-kernel",
        "story-engine",
    }
    invalidated: list[str] = []
    for question_id, answer in answers.items():
        if question_id not in known_questions:
            raise PlotInitError(
                "UNKNOWN_QUESTION",
                f"unknown initialization question: {question_id}",
            )
        delegated = False
        selected_option: dict[str, Any] | None = None
        patch: dict[str, Any]
        raw_answer = copy.deepcopy(answer)
        if isinstance(answer, dict) and isinstance(answer.get("patch"), dict):
            patch = copy.deepcopy(answer["patch"])
            delegated = bool(answer.get("delegated_choice"))
        else:
            if isinstance(answer, dict):
                option_id = str(answer.get("option_id") or answer.get("value") or "")
                delegated = bool(answer.get("delegated_choice"))
            else:
                option_id = str(answer or "").strip()
            if option_id in {"你来定", "交给你", "默认", "按推荐"}:
                delegated = True
                option_id = ""
            package = _question_package(state, question_id)
            if not option_id and delegated:
                option_id = str(package["default_option_id"])
            selected_option = next(
                (
                    option
                    for option in package["options"]
                    if str(option["option_id"]) == option_id
                ),
                None,
            )
            if selected_option is not None:
                patch = copy.deepcopy(selected_option["patch"])
            else:
                patch = _natural_answer_patch(question_id, option_id, state)

        old_hash = canonical_hash(state.get("answer_patches", {}).get(question_id))
        new_hash = canonical_hash(patch)
        if old_hash != new_hash:
            dependencies = QUESTION_DEPENDENCIES[question_id]
            invalidated.extend(dependencies)
            downstream_questions = {
                "genre-contract": (
                    "world-causal-kernel",
                    "story-engine",
                    "derived-serialization",
                ),
                "world-causal-kernel": ("story-engine", "derived-serialization"),
                "power-causal-kernel": ("story-engine", "derived-serialization"),
                "story-engine": ("derived-serialization",),
            }
            for dependent_question in downstream_questions[question_id]:
                state["answer_patches"].pop(dependent_question, None)
                state["answer_field_states"].pop(dependent_question, None)
                state["answers"].pop(dependent_question, None)
        state["answer_patches"][question_id] = patch
        state["answer_field_states"][question_id] = _states_for_payload(
            patch,
            field_status="user_confirmed",
            origin="model_suggestion" if selected_option is not None else "user_input",
            decision_status="delegated" if delegated else "session_locked",
            source_refs=[f"answer:{question_id}:{canonical_hash(raw_answer)[:16]}"],
            confidence=1.0,
            depends_on=[question_id],
            invalidates=list(QUESTION_DEPENDENCIES[question_id]),
        )
        state["answers"][question_id] = raw_answer
        state["decisions"].append(
            {
                "decision_id": stable_id(
                    "decision",
                    state["session_id"],
                    question_id,
                    new_hash,
                ),
                "question_id": question_id,
                "selected_option_id": (
                    selected_option.get("option_id") if selected_option else None
                ),
                "delegated_choice": delegated,
                "origin": (
                    "model_suggestion" if selected_option is not None else "user_input"
                ),
                "answer_hash": canonical_hash(raw_answer),
                "patch_hash": new_hash,
                "session_revision": state["session_revision"],
            }
        )
    state["invalidated_nodes"] = sorted(set(invalidated))
    state["bundle"] = None
    state["bundle_hash"] = None
    state["proposal_id"] = None
    state["current_questions"] = []
    _compose_dossier(state)
    return state, sorted(set(invalidated))


def build_proposal(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    state = copy.deepcopy(state)
    if state.get("status") == "PROPOSAL_FROZEN" and state.get("proposal"):
        return state, copy.deepcopy(state["proposal"])
    if state.get("stage") != "READY_TO_PROPOSE" or state.get("status") != "READY_TO_PROPOSE":
        raise PlotInitError(
            "NOT_READY_TO_PROPOSE",
            "initialization session must pass review before proposal freeze",
            stage=state.get("stage"),
            status=state.get("status"),
        )
    bundle = build_bundle(state)
    if not bundle["validation"]["proposal_ready"]:
        raise PlotInitError(
            "BLOCKING_GAPS",
            "initialization bundle still has blocking gaps",
            blocking_gap_count=bundle["validation"]["blocking_gap_count"],
        )
    package_hash = str(bundle["bundle_hash"])
    proposal_id = stable_id(
        "initp",
        package_hash,
        state.get("project_root") or state.get("workspace_root"),
        state.get("expected_canon_revision"),
        state["session_id"],
    )
    item_sidecar = item_package_from_artifact_manifest(
        item
        for item in bundle["artifact_manifest"]
        if isinstance(item, Mapping)
    )
    item_sidecar_reference_value = (
        item_sidecar[1] if item_sidecar is not None else None
    )
    advantage_sidecar = advantage_package_from_artifact_manifest(
        item
        for item in bundle["artifact_manifest"]
        if isinstance(item, Mapping)
    )
    advantage_sidecar_reference_value = (
        advantage_sidecar[1]
        if advantage_sidecar is not None
        else None
    )
    proposal = {
        "schema_version": str(bundle.get("schema_version") or PROTOCOL_V1),
        "proposal_id": proposal_id,
        "package_hash": package_hash,
        "status": "PROPOSAL_FROZEN",
        "session_ref": {
            "session_id": state["session_id"],
            "session_revision": state["session_revision"],
        },
        "target_project_real_path": state.get("project_root"),
        "source_manifest_hash": canonical_hash(bundle["source_manifest"]),
        "bundle": bundle,
        "proposed_canon_deltas": [
            {
                "claim_id": claim["claim_id"],
                "scope": claim.get("scope"),
                "knowledge_plane": claim.get("knowledge_plane"),
                "canon_status": "proposed",
            }
            for claim in bundle["provenance"]["claims"]
            if claim.get("authority_tier") in {"T0", "T1", "T2", "T3"}
            and claim.get("modality") == "asserted"
        ],
        "apply_plan": {
            "available_from_version": "0.5.0",
            "requires_approval_grant": True,
            "authorized_operations_required": [
                "accept_initialization",
                "materialize",
            ],
            "artifacts": [
                {
                    "artifact_id": item["artifact_id"],
                    "path": item["path"],
                    "operation": item["operation"],
                    "expected_old_hash": item["expected_old_hash"],
                    "proposed_new_hash": item["proposed_new_hash"],
                }
                for item in bundle["artifact_manifest"]
            ],
            **(
                {"item_sidecar": item_sidecar_reference_value}
                if item_sidecar_reference_value is not None
                else {}
            ),
            **(
                {"advantage_sidecar": advantage_sidecar_reference_value}
                if advantage_sidecar_reference_value is not None
                else {}
            ),
            "executed": False,
        },
        "validation": copy.deepcopy(bundle["validation"]),
        "created_at": utc_now(),
    }
    state["proposal_id"] = proposal_id
    state["proposal"] = proposal
    state["stage"] = "PROPOSAL_FROZEN"
    state["status"] = "PROPOSAL_FROZEN"
    state["current_questions"] = []
    state["updated_at"] = utc_now()
    return state, proposal


def public_session(state: dict[str, Any], *, include_bundle: bool = False) -> dict[str, Any]:
    payload = {
        key: copy.deepcopy(value)
        for key, value in state.items()
        if key
        not in {
            "seed_dossier",
            "seed_field_states",
            "source_dossiers",
            "source_field_states",
            "answer_patches",
            "answer_field_states",
            "claims_by_source",
            "normalized_exports",
            "dossier",
            "proposal",
        }
    }
    if include_bundle:
        payload["bundle"] = copy.deepcopy(state.get("bundle"))
    return payload


def response_for_state(
    state: dict[str, Any],
    *,
    operation: str,
    idempotent: bool = False,
    include_bundle: bool = False,
) -> dict[str, Any]:
    payload = public_session(state, include_bundle=include_bundle)
    payload.update(
        {
            "operation": operation,
            "idempotent": idempotent,
            "status": state["status"],
            "session_id": state["session_id"],
            "session_revision": state["session_revision"],
            "stage": state["stage"],
        }
    )
    if state.get("proposal"):
        payload["proposal"] = copy.deepcopy(state["proposal"])
    return payload
