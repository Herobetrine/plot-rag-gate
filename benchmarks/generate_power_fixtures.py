#!/usr/bin/env python3
"""Generate the versioned 360-case typed power Stop benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


PROFILES = (
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
PROFILE_PROBES = {
    "cultivation": "筑基金丹天劫灵根与修行境界",
    "magic": "法术位咒语魔杖专注与学派施法",
    "skill_tree": "前置技能技能点洗点与互斥分支",
    "game": "任务奖励副本转职属性点与声望",
    "martial": "经脉内力拳法刀法身法与武意",
    "superpower": "异能觉醒过载污染与抑制器",
    "bloodline": "返祖血脉纯度祭血遗传与排异",
    "technology": "义体机甲能源热量算力与带宽",
    "contract_summoning": "契约位御兽控制距离共享伤害与反噬",
    "system_assist": "系统面板宿主商城权限点与模块解锁",
    "hybrid": "跨体系换算桥接规则命名空间与隔离",
    "mundane": "现实题材没有超能力且明确无超凡",
}
OUTPUT = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "power_system_annotations.v1.jsonl"
)


def _delta(
    *,
    event_type: str,
    action: str,
    subject: str,
    object_value: str | None,
    field: str | None,
    value: Any,
    evidence: str,
    knowledge_plane: str = "objective",
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "action": action,
        "subject": subject,
        "object": object_value,
        "field": field,
        "value": value,
        "scope": "current",
        "effective_at": None,
        "story_coordinate": {
            "calendar_id": "benchmark",
            "ordinal": 1,
            "label": "基准时点一",
            "precision": "scene",
        },
        "knowledge_plane": knowledge_plane,
        "confidence": 0.99,
        "evidence": evidence,
    }


def _accepted_case(
    profile: str,
    ordinal: int,
    family: str,
) -> dict[str, Any]:
    actor = f"{profile}_角色_{ordinal:02d}"
    target = f"{profile}_目标_{ordinal:02d}"
    if family == "ability":
        object_value = f"{profile}_能力_{ordinal:02d}"
        text = f"{actor}获得了{object_value}。"
        delta = _delta(
            event_type="ability",
            action="gain",
            subject=actor,
            object_value=object_value,
            field="ability",
            value={"level": "初阶", "source": "剧情奖励"},
            evidence=text,
        )
    elif family == "resource":
        object_value = f"{profile}_资源_{ordinal:02d}"
        text = f"{actor}的{object_value}被初始化为十点。"
        delta = _delta(
            event_type="resource",
            action="initialize",
            subject=actor,
            object_value=object_value,
            field="balance",
            value={"amount": 10, "source": "bootstrap"},
            evidence=text,
        )
    elif family == "status_effect":
        object_value = f"{profile}_状态_{ordinal:02d}"
        text = f"{actor}进入了{object_value}状态。"
        delta = _delta(
            event_type="status_effect",
            action="apply",
            subject=actor,
            object_value=object_value,
            field="active",
            value={"stacks": 1},
            evidence=text,
        )
    elif family == "power_binding":
        object_value = f"{profile}_媒介_{ordinal:02d}"
        text = f"{actor}绑定了{object_value}。"
        delta = _delta(
            event_type="power_binding",
            action="bind",
            subject=actor,
            object_value=object_value,
            field="source",
            value={
                "binding_id": f"{profile}:binding:{ordinal:02d}",
                "source_type": "item",
                "ability_ids": [],
                "unique": True,
            },
            evidence=text,
        )
    elif family == "qualification":
        object_value = f"{profile}_资格_{ordinal:02d}"
        text = f"{actor}获得了{object_value}。"
        delta = _delta(
            event_type="qualification",
            action="grant",
            subject=actor,
            object_value=object_value,
            field="qualification",
            value={"quantity": 1},
            evidence=text,
        )
    else:
        observer = actor
        observed = target
        ability = f"{profile}_被观察能力_{ordinal:02d}"
        text = f"{observer}亲眼看见{observed}用{ability}释放出火焰。"
        delta = _delta(
            event_type="power_observation",
            action="observe",
            subject=observer,
            object_value=observed,
            field="effect",
            value={
                "ability": ability,
                "observed_fields": {"effect": "释放火焰"},
            },
            evidence=text,
            knowledge_plane="actor_belief",
        )
    return {
        "manifest_version": 1,
        "suite": "plot-rag-power",
        "case_id": f"{profile}-accepted-{ordinal:02d}",
        "profile": profile,
        "profile_probe": PROFILE_PROBES[profile],
        "case_kind": "accepted",
        "assistant_text": text,
        "stop_envelope": {
            "schema_version": "plot-rag-delta/v3",
            "deltas": [delta],
        },
        "expected_status": "accepted",
        "expected_event_type": delta["event_type"],
        "coverage_tags": [
            *(
                ["knowledge_boundary"]
                if family == "power_observation"
                else []
            ),
            "replay",
        ],
    }


def _dangerous_case(
    profile: str,
    ordinal: int,
    family: str,
) -> dict[str, Any]:
    actor = f"{profile}_危险角色_{ordinal:02d}"
    object_value = f"{profile}_危险对象_{ordinal:02d}"
    cross_system = 13 <= ordinal <= 17
    if cross_system:
        target_profile = PROFILES[
            (
                PROFILES.index(profile)
                + 1
                + (ordinal - 13)
            )
            % len(PROFILES)
        ]
        source_resource = f"{profile}_源体系资源_{ordinal:02d}"
        target_resource = (
            f"{target_profile}_目标体系资源_{ordinal:02d}"
        )
        missing_rule = (
            f"{profile}_到_{target_profile}_缺失换算规则_{ordinal:02d}"
        )
        text = (
            f"{actor}试图在没有任何桥接规则的情况下，把"
            f"{source_resource}直接转换成{target_resource}。"
        )
        delta = _delta(
            event_type="resource",
            action="convert",
            subject=actor,
            object_value=source_resource,
            field="balance",
            value={
                "amount": 5,
                "target_resource": target_resource,
                "target_amount": 5,
                "conversion_rule": missing_rule,
                "source": "跨体系强制转换",
            },
            evidence=text,
        )
    elif family == "ability":
        text = f"{actor}直接使用了从未获得的{object_value}。"
        delta = _delta(
            event_type="ability",
            action="use",
            subject=actor,
            object_value=object_value,
            field="ability",
            value={"effect": "强行发动"},
            evidence=text,
        )
    elif family == "resource":
        text = f"{actor}从尚未建立的{object_value}中消耗了五点。"
        delta = _delta(
            event_type="resource",
            action="spend",
            subject=actor,
            object_value=object_value,
            field="balance",
            value={"amount": 5, "source": "施法"},
            evidence=text,
        )
    elif family == "status_effect":
        text = f"{actor}移除了从未施加的{object_value}。"
        delta = _delta(
            event_type="status_effect",
            action="remove",
            subject=actor,
            object_value=object_value,
            field="active",
            value={"stacks": 1},
            evidence=text,
        )
    elif family == "power_binding":
        text = f"{actor}解除了从未建立的{object_value}绑定。"
        delta = _delta(
            event_type="power_binding",
            action="unbind",
            subject=actor,
            object_value=object_value,
            field="source",
            value={
                "binding_id": f"{profile}:missing-binding:{ordinal:02d}",
                "source_type": "item",
                "ability_ids": [],
            },
            evidence=text,
        )
    elif family == "qualification":
        text = f"{actor}消耗了从未获得的{object_value}。"
        delta = _delta(
            event_type="qualification",
            action="consume",
            subject=actor,
            object_value=object_value,
            field="qualification",
            value={"quantity": 1},
            evidence=text,
        )
    else:
        rank = f"{profile}_目标阶位_{ordinal:02d}"
        text = f"{actor}没有晋升边便直接突破到{rank}。"
        delta = _delta(
            event_type="progression",
            action="advance",
            subject=actor,
            object_value=object_value,
            field="track",
            value={"to_rank": rank},
            evidence=text,
        )
    record = {
        "manifest_version": 1,
        "suite": "plot-rag-power",
        "case_id": f"{profile}-quarantine-{ordinal:02d}",
        "profile": profile,
        "profile_probe": PROFILE_PROBES[profile],
        "case_kind": "dangerous",
        "assistant_text": text,
        "stop_envelope": {
            "schema_version": "plot-rag-delta/v3",
            "deltas": [delta],
        },
        "expected_status": "quarantined",
        "expected_event_type": delta["event_type"],
        "coverage_tags": (
            ["cross_system_dangerous"]
            if cross_system
            else []
        ),
    }
    if cross_system:
        record["power_setup"] = {
            "systems": [
                {
                    "ref": "source",
                    "name": f"{profile}_力量体系",
                    "profile": profile,
                    "namespace": f"benchmark:{profile}",
                },
                {
                    "ref": "target",
                    "name": f"{target_profile}_力量体系",
                    "profile": target_profile,
                    "namespace": f"benchmark:{target_profile}",
                },
            ],
            "resources": [
                {
                    "name": source_resource,
                    "system_ref": "source",
                },
                {
                    "name": target_resource,
                    "system_ref": "target",
                },
            ],
            "bridge_rules": [],
            "conversion_rules": [],
        }
    return record


def build_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    accepted_families = (
        "power_observation",
        "power_observation",
        "power_observation",
        "power_observation",
        "ability",
        "ability",
        "resource",
        "resource",
        "status_effect",
        "power_binding",
        "qualification",
        "qualification",
    )
    dangerous_families = (
        "ability",
        "resource",
        "status_effect",
        "power_binding",
        "qualification",
        "progression",
    )
    for profile in PROFILES:
        for ordinal in range(1, 13):
            records.append(
                _accepted_case(
                    profile,
                    ordinal,
                    accepted_families[(ordinal - 1) % len(accepted_families)],
                )
            )
        for ordinal in range(13, 28):
            records.append(
                _dangerous_case(
                    profile,
                    ordinal,
                    dangerous_families[(ordinal - 13) % len(dangerous_families)],
                )
            )
        for ordinal in range(28, 31):
            text = f"{profile}_角色_{ordinal:02d}与同伴沉默对视片刻。"
            records.append(
                {
                    "manifest_version": 1,
                    "suite": "plot-rag-power",
                    "case_id": f"{profile}-zero-{ordinal:02d}",
                    "profile": profile,
                    "profile_probe": PROFILE_PROBES[profile],
                    "case_kind": "zero_delta",
                    "assistant_text": text,
                    "stop_envelope": {
                        "schema_version": "plot-rag-delta/v3",
                        "deltas": [],
                    },
                    "expected_status": "zero_delta",
                    "expected_event_type": None,
                }
            )
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    records = build_records()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "cases": len(records),
                "profiles": len(PROFILES),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
