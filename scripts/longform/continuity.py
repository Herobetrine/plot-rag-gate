from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence

from .authority import AuthorityIndex


_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "power_state",
        (
            "战斗",
            "追逐",
            "能力",
            "技能",
            "法术",
            "术式",
            "功法",
            "境界",
            "突破",
            "升级",
            "训练",
            "领悟",
            "装备",
            "炼制",
            "系统任务",
            "奖励",
            "契约",
            "召唤",
            "冷却",
            "力量体系",
            "combat",
            "ability",
            "skill",
            "power",
            "level up",
        ),
    ),
    (
        "progression",
        (
            "境界",
            "突破",
            "晋升",
            "升级",
            "转职",
            "技能树",
            "训练",
            "领悟",
            "progression",
            "rank",
            "level",
        ),
    ),
    (
        "ability",
        (
            "能力",
            "技能",
            "法术",
            "术式",
            "招式",
            "冷却",
            "蓄力",
            "能力代价",
            "ability",
            "spell",
            "skill",
            "cooldown",
        ),
    ),
    (
        "resource",
        (
            "法力",
            "灵力",
            "体力",
            "经验",
            "弹药",
            "热量",
            "算力",
            "寿命",
            "资源",
            "消耗",
            "恢复",
            "resource",
            "mana",
            "energy",
        ),
    ),
    (
        "power_binding",
        (
            "装备",
            "法器",
            "义体",
            "契约",
            "召唤",
            "血脉",
            "同调",
            "绑定",
            "binding",
            "equipment",
            "contract",
            "summon",
        ),
    ),
    (
        "location",
        ("位置", "地点", "在哪", "进入", "离开", "赶到", "移动", "location", "where"),
    ),
    (
        "inventory",
        ("道具", "持有", "获得", "消耗", "资源", "法器", "inventory", "item"),
    ),
    (
        "relationship",
        ("关系", "信任", "敌意", "盟友", "亲缘", "relationship", "trust"),
    ),
    (
        "story_time",
        ("时间", "期限", "几天", "当晚", "次日", "story time", "deadline"),
    ),
    (
        "open_loop",
        ("伏笔", "承诺", "悬念", "钩子", "兑现", "未解决", "open loop", "payoff"),
    ),
)
_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")


@dataclass(frozen=True)
class ContinuityNeed:
    category: str
    query: str
    mandatory: bool
    reason: str


def decompose_continuity_needs(prompt: str) -> list[ContinuityNeed]:
    """Split a prompt into one to five deterministic atomic continuity needs."""

    normalized = " ".join(prompt.split())
    if not normalized:
        normalized = "继续当前剧情"
    clauses = [clause.strip() for clause in _SPLIT_RE.split(normalized) if clause.strip()]
    base_clause = clauses[0] if clauses else normalized
    needs: list[ContinuityNeed] = [
        ContinuityNeed(
            category="current_state",
            query=f"{base_clause} 当前角色状态 当前目标 当前伤势",
            mandatory=True,
            reason="任何剧情生成都必须承接当前有效状态",
        )
    ]
    folded = normalized.casefold()
    for category, keywords in _CATEGORY_RULES:
        if any(keyword.casefold() in folded for keyword in keywords):
            matching_clause = next(
                (
                    clause
                    for clause in clauses
                    if any(keyword.casefold() in clause.casefold() for keyword in keywords)
                ),
                base_clause,
            )
            needs.append(
                ContinuityNeed(
                    category=category,
                    query=f"{matching_clause} {category}",
                    mandatory=category
                    in {
                        "open_loop",
                        "power_state",
                        "progression",
                        "ability",
                        "resource",
                        "power_binding",
                    },
                    reason=f"prompt 明确涉及 {category}",
                )
            )
        if len(needs) == 5:
            break
    if len(needs) == 1 and len(clauses) > 1:
        for clause in clauses[1:5]:
            needs.append(
                ContinuityNeed(
                    category="continuity",
                    query=clause,
                    mandatory=False,
                    reason="prompt 中的独立剧情约束",
                )
            )
    return needs[:5]


class ContextContractBuilder:
    """Build a bounded context contract with mandatory category reservations."""

    DEFAULT_QUOTAS = {
        "accepted_authority": 1,
        "current_state": 2,
        "open_loop": 2,
        "power_state": 2,
        "progression": 1,
        "ability": 1,
        "resource": 1,
        "power_binding": 1,
    }
    TASK_ROLE_ORDER = {
        "outline": ("canon", "setting", "outline"),
        "scene": ("canon", "setting"),
        "prose": ("canon", "setting"),
        "revision": ("canon", "setting", "outline"),
    }
    TASK_RECALL_ORDER = {
        "outline": (
            ("summary", "volume"),
            ("summary", "arc"),
            ("memory", "semantic"),
            ("memory", "episodic"),
            ("summary", "chapter"),
        ),
        "scene": (
            ("memory", "episodic"),
            ("summary", "chapter"),
            ("summary", "arc"),
            ("memory", "semantic"),
            ("summary", "volume"),
        ),
        "prose": (
            ("memory", "episodic"),
            ("summary", "chapter"),
            ("memory", "semantic"),
            ("summary", "arc"),
            ("summary", "volume"),
        ),
        "revision": (
            ("summary", "chapter"),
            ("memory", "episodic"),
            ("summary", "arc"),
            ("memory", "semantic"),
            ("summary", "volume"),
        ),
    }

    def __init__(
        self,
        authority_index: AuthorityIndex,
        *,
        memory_store: Any | None = None,
        summary_store: Any | None = None,
    ) -> None:
        self.authority_index = authority_index
        self.memory_store = memory_store
        self.summary_store = summary_store

    @staticmethod
    def _render_item(item: Mapping[str, Any]) -> str:
        label = item.get("category") or item.get("role") or "context"
        source = item.get("source") or item.get("path") or item.get("layer") or "derived"
        text = " ".join(str(item.get("text") or item.get("content") or "").split())
        coordinates: list[str] = []
        for key, coordinate_label in (
            ("scope", "scope"),
            ("chapter_no", "chapter"),
            ("arc_id", "arc"),
            ("volume_id", "volume"),
        ):
            value = item.get(key)
            if value not in {None, ""}:
                coordinates.append(f"{coordinate_label}={value}")
        suffix = "|" + "|".join(coordinates) if coordinates else ""
        return f"[{label}|{source}{suffix}] {text}".strip()

    def _memory_candidates(
        self,
        prompt: str,
        category: str,
        limit: int,
        *,
        branch_id: str | None,
        chapter_no: int | None,
        arc_id: str | None,
        volume_id: str | None,
    ) -> list[dict[str, Any]]:
        if self.memory_store is None:
            return []
        rows = self.memory_store.query(
            prompt,
            categories=[category],
            branch_id=branch_id,
            chapter_no=chapter_no,
            arc_id=arc_id,
            volume_id=volume_id,
            limit=limit,
        )
        return [
            {
                **row,
                "category": category,
                "source": f"memory:{row['layer']}",
            }
            for row in rows
        ]

    def _supplemental_memory_candidates(
        self,
        prompt: str,
        layer: str,
        limit: int,
        *,
        branch_id: str | None,
        chapter_no: int | None,
        arc_id: str | None,
        volume_id: str | None,
    ) -> list[dict[str, Any]]:
        if self.memory_store is None:
            return []
        categories = {
            "episodic": ("chapter_summary", "event"),
            "semantic": ("world_rule",),
        }.get(layer, ())
        if not categories:
            return []
        rows = self.memory_store.query(
            prompt,
            layers=[layer],
            categories=categories,
            branch_id=branch_id,
            chapter_no=chapter_no,
            arc_id=arc_id,
            volume_id=volume_id,
            limit=max(limit * 4, limit),
        )
        relevant = [
            row
            for row in rows
            if float(row.get("lexical_score") or 0.0) > 0.0
            or float(row.get("context_score") or 0.0) > 0.0
        ]
        return [
            {
                **row,
                "memory_category": row.get("category"),
                "category": f"{layer}_memory",
                "source": f"memory:{layer}",
            }
            for row in relevant[:limit]
        ]

    def _summary_candidates(
        self,
        prompt: str,
        level: str,
        limit: int,
        *,
        branch_id: str | None,
        chapter_no: int | None,
        arc_id: str | None,
        volume_id: str | None,
    ) -> list[dict[str, Any]]:
        if self.summary_store is None:
            return []
        rows = self.summary_store.query(
            prompt,
            levels=[level],
            branch_id=branch_id,
            chapter_no=chapter_no,
            arc_id=arc_id,
            volume_id=volume_id,
            limit=limit,
        )
        return [
            {
                **row,
                "category": f"{level}_summary",
                "source": f"summary:{level}",
            }
            for row in rows
        ]

    @staticmethod
    def _aggregate_retrieval_diagnostics(
        diagnostics: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if not diagnostics:
            return {}
        summed_fields = {
            "query_count",
            "candidate_cache_hits",
            "search_singleflight_waits",
            "embedding_cache_hits",
            "embedding_singleflight_waits",
            "embedding_batch_calls",
            "embedding_batch_failures",
            "embedding_batch_ms",
            "embedding_single_fallbacks",
            "embedding_single_calls",
            "embedding_single_ms",
            "embedding_single_wall_ms",
            "rerank_wall_ms",
            "rerank_sum_ms",
            "rerank_cache_hits",
            "rerank_singleflight_waits",
            "rerank_cache_misses",
            "cache_hit_count",
            "cache_miss_count",
            "authority_search_ms",
        }
        result: dict[str, Any] = {
            field: sum(
                float(item.get(field) or 0.0)
                for item in diagnostics
            )
            for field in summed_fields
        }
        for field in {
            "query_count",
            "candidate_cache_hits",
            "search_singleflight_waits",
            "embedding_cache_hits",
            "embedding_singleflight_waits",
            "embedding_batch_calls",
            "embedding_batch_failures",
            "embedding_single_fallbacks",
            "embedding_single_calls",
            "rerank_cache_hits",
            "rerank_singleflight_waits",
            "rerank_cache_misses",
            "cache_hit_count",
            "cache_miss_count",
        }:
            result[field] = int(result[field])
        result["rerank_max_concurrency"] = max(
            int(item.get("rerank_max_concurrency") or 1)
            for item in diagnostics
        )
        result["embedding_single_max_concurrency"] = max(
            int(item.get("embedding_single_max_concurrency") or 1)
            for item in diagnostics
        )
        result["queries"] = [
            dict(query)
            for item in diagnostics
            for query in (item.get("queries") or [])
            if isinstance(query, Mapping)
        ]
        result["search_calls"] = len(diagnostics)
        return result

    def build(
        self,
        prompt: str,
        *,
        task: str = "prose",
        max_context_chars: int = 12000,
        category_quotas: Mapping[str, int] | None = None,
        authority_limit: int = 12,
        branch_id: str | None = "main",
        chapter_no: int | None = None,
        arc_id: str | None = None,
        volume_id: str | None = None,
        search_mode: str = "auto",
        use_candidate_cache: bool = True,
        skip_authority_need_indices: Iterable[int] = (),
        exact_state_satisfied_counts: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        budget = max(1, int(max_context_chars))
        normalized_search_mode = str(search_mode or "auto").strip().lower()
        if normalized_search_mode not in {"auto", "legacy", "v2"}:
            raise ValueError(
                "search_mode must be one of auto, legacy, or v2"
            )
        if type(use_candidate_cache) is not bool:
            raise ValueError("use_candidate_cache must be boolean")
        quotas = dict(self.DEFAULT_QUOTAS)
        if category_quotas:
            quotas.update(
                {
                    str(key): max(0, int(value))
                    for key, value in category_quotas.items()
                }
            )
        needs = decompose_continuity_needs(prompt)
        skipped_need_indices = {
            int(value) for value in skip_authority_need_indices
        }
        invalid_skipped = sorted(
            value
            for value in skipped_need_indices
            if value < 0 or value >= len(needs)
        )
        if invalid_skipped:
            raise ValueError(
                "skip_authority_need_indices contains invalid indices: "
                + ", ".join(str(value) for value in invalid_skipped)
            )
        exact_satisfied = {
            str(category): max(0, int(count))
            for category, count in dict(
                exact_state_satisfied_counts or {}
            ).items()
        }
        roles = self.TASK_ROLE_ORDER.get(task, ("canon", "setting"))
        search_requests = [
            {
                "need_index": need_index,
                "category": need.category,
                "query": need.query,
                "limit": int(authority_limit),
                "roles": roles,
                "scope_policies": None,
                "ingest_policies": ("include", "review"),
                "use_candidate_cache": use_candidate_cache,
            }
            for need_index, need in enumerate(needs)
            if need_index not in skipped_need_indices
        ]
        search_many = getattr(self.authority_index, "search_many", None)
        retrieval_telemetry: dict[str, Any] = {}
        searched_results: list[list[dict[str, Any]]]
        if not search_requests:
            searched_results = []
        elif normalized_search_mode == "v2":
            if not callable(search_many):
                raise ValueError(
                    "search_mode=v2 requires AuthorityIndex.search_many"
                )
            searched_results = list(search_many(search_requests))
        elif normalized_search_mode == "legacy" or not callable(search_many):
            sequential_diagnostics: list[dict[str, Any]] = []
            searched_results = [
                []
                for _request in search_requests
            ]
            for request_index, request in enumerate(search_requests):
                searched_results[request_index] = (
                    self.authority_index.search(
                        str(request["query"]),
                        limit=int(request["limit"]),
                        roles=request["roles"],
                        scope_policies=request["scope_policies"],
                        ingest_policies=request["ingest_policies"],
                        use_candidate_cache=bool(
                            request["use_candidate_cache"]
                        ),
                    )
                )
                last_diagnostics = getattr(
                    self.authority_index,
                    "last_search_diagnostics",
                    None,
                )
                if callable(last_diagnostics):
                    diagnostic = last_diagnostics()
                    if isinstance(diagnostic, Mapping):
                        sequential_diagnostics.append(dict(diagnostic))
            retrieval_telemetry = self._aggregate_retrieval_diagnostics(
                sequential_diagnostics
            )
        else:
            searched_results = list(search_many(search_requests))
        if search_requests and not retrieval_telemetry:
            last_diagnostics = getattr(
                self.authority_index,
                "last_search_diagnostics",
                None,
            )
            if callable(last_diagnostics):
                diagnostic = last_diagnostics()
                if isinstance(diagnostic, Mapping):
                    retrieval_telemetry = dict(diagnostic)
        if len(searched_results) != len(search_requests):
            raise ValueError(
                "authority search_many result count does not match needs"
            )
        authority_by_need: list[list[dict[str, Any]]] = [
            [] for _need in needs
        ]
        for request, candidates in zip(search_requests, searched_results):
            need_index = int(request["need_index"])
            authority_by_need[need_index] = [
                {
                    **dict(candidate),
                    "need_index": need_index,
                    "category": needs[need_index].category,
                }
                for candidate in candidates
            ]
        retrieval_telemetry.update(
            {
                "skipped_exact_need_count": len(skipped_need_indices),
                "skipped_exact_need_indices": sorted(
                    skipped_need_indices
                ),
                "skipped_exact_categories": sorted(
                    {
                        needs[index].category
                        for index in skipped_need_indices
                    }
                ),
            }
        )
        sections: dict[str, list[dict[str, Any]]] = {}
        missing_mandatory: list[str] = []
        mandatory_shortfall: dict[str, int] = {}
        selected_ids: set[str] = set()
        rendered: list[str] = []
        used_chars = 0

        def append_item(
            category: str,
            item: dict[str, Any],
            *,
            protected_chars: int = 0,
        ) -> bool:
            nonlocal used_chars
            stable_id = str(
                item.get("memory_id")
                or item.get("chunk_id")
                or item.get("content_sha256")
                or self._render_item(item)
            )
            if stable_id in selected_ids:
                return False
            line = self._render_item(item)
            separator = 1 if rendered else 0
            remaining = (
                budget
                - used_chars
                - separator
                - max(0, int(protected_chars))
            )
            if remaining <= 0:
                return False
            if len(line) > remaining:
                if remaining < 48:
                    return False
                line = line[: max(1, remaining - 1)].rstrip() + "…"
                item = {**item, "truncated": True}
            rendered.append(line)
            used_chars += len(line) + separator
            selected_ids.add(stable_id)
            sections.setdefault(category, []).append(item)
            return True

        authority_quota = max(
            0,
            int(quotas.get("accepted_authority", 1)),
        )
        exact_authority_satisfied = min(
            authority_quota,
            int(exact_satisfied.get("accepted_authority") or 0),
        )
        reserved_authority: list[dict[str, Any]] = []
        reserved_authority_ids: set[str] = set()
        for need_index, need in enumerate(needs):
            candidates = authority_by_need[need_index]
            for candidate in candidates:
                stable_id = str(
                    candidate.get("chunk_id")
                    or candidate.get("content_sha256")
                    or self._render_item(candidate)
                )
                if stable_id in reserved_authority_ids:
                    continue
                reserved_authority_ids.add(stable_id)
                reserved_authority.append(
                    {
                        **candidate,
                        "category": need.category,
                        "authority_reservation": "accepted_task_authority",
                    }
                )
        authority_reserve_chars = 0
        if authority_quota > 0 and reserved_authority:
            authority_reserve_chars = min(
                1200,
                max(48, budget // 4),
                max(0, budget - 96),
            )

        mandatory_categories = ["current_state", "open_loop"]
        mandatory_categories.extend(
            need.category
            for need in needs
            if need.mandatory
            and need.category not in {"current_state", "open_loop"}
        )
        for category in dict.fromkeys(mandatory_categories):
            quota = quotas.get(category, 0)
            accepted = min(
                max(0, int(quota)),
                int(exact_satisfied.get(category) or 0),
            )
            candidates = self._memory_candidates(
                prompt,
                category,
                max((quota - accepted) * 4, quota - accepted),
                branch_id=branch_id,
                chapter_no=chapter_no,
                arc_id=arc_id,
                volume_id=volume_id,
            )
            for candidate in candidates:
                if accepted >= quota:
                    break
                if append_item(
                    category,
                    candidate,
                    protected_chars=authority_reserve_chars,
                ):
                    accepted += 1
            if accepted == 0 and quota > 0:
                missing_mandatory.append(category)
            if accepted < quota:
                mandatory_shortfall[category] = quota - accepted

        authority_selected = 0
        for candidate in reserved_authority:
            if authority_selected >= authority_quota:
                break
            if append_item(str(candidate["category"]), candidate):
                authority_selected += 1
        authority_satisfied = min(
            authority_quota,
            exact_authority_satisfied + authority_selected,
        )
        if authority_satisfied == 0 and authority_quota > 0:
            missing_mandatory.append("accepted_authority")
        if authority_satisfied < authority_quota:
            mandatory_shortfall["accepted_authority"] = (
                authority_quota - authority_satisfied
            )

        for need_index, need in enumerate(needs):
            if used_chars >= budget:
                break
            candidates = authority_by_need[need_index]
            for candidate in candidates:
                candidate = {
                    **candidate,
                    "need_index": need_index,
                    "category": need.category,
                }
                if append_item(need.category, candidate):
                    break

        for source_kind, name in self.TASK_RECALL_ORDER.get(
            task,
            self.TASK_RECALL_ORDER["prose"],
        ):
            if used_chars >= budget:
                break
            if source_kind == "memory":
                candidates = self._supplemental_memory_candidates(
                    prompt,
                    name,
                    2,
                    branch_id=branch_id,
                    chapter_no=chapter_no,
                    arc_id=arc_id,
                    volume_id=volume_id,
                )
            else:
                candidates = self._summary_candidates(
                    prompt,
                    name,
                    1,
                    branch_id=branch_id,
                    chapter_no=chapter_no,
                    arc_id=arc_id,
                    volume_id=volume_id,
                )
            for candidate in candidates:
                append_item(str(candidate["category"]), candidate)

        return {
            "contract_version": 1,
            "task": task,
            "needs": [
                {"need_index": need_index, **asdict(need)}
                for need_index, need in enumerate(needs)
            ],
            "mandatory_quotas": quotas,
            "missing_mandatory": sorted(set(missing_mandatory)),
            "mandatory_shortfall": mandatory_shortfall,
            "accepted_authority_selected": authority_selected,
            "accepted_authority_satisfied": authority_satisfied,
            "accepted_authority_reserve_chars": authority_reserve_chars,
            "exact_state_short_circuit": {
                "skipped_need_indices": sorted(skipped_need_indices),
                "skipped_categories": sorted(
                    {
                        needs[index].category
                        for index in skipped_need_indices
                    }
                ),
                "satisfied_mandatory_counts": exact_satisfied,
            },
            "sections": sections,
            "context_text": "\n".join(rendered),
            "context_chars": used_chars,
            "max_context_chars": budget,
            "within_budget": used_chars <= budget,
            "retrieval_telemetry": retrieval_telemetry,
        }
