from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
ANNOTATION_PATH = FIXTURES / "longform_annotations.v1.jsonl"
CHAPTER_PATH = FIXTURES / "chapters_500.v1.jsonl"
GENRES = ("玄幻", "仙侠", "科幻", "悬疑", "都市")
TASKS = ("outline", "scene", "prose", "revision")
CATEGORIES = ("location", "inventory", "story_time", "relation")
OPEN_MARKER = "<plot-delta>"
CLOSE_MARKER = "</plot-delta>"

CHARACTERS = (
    {
        "entity_id": "character:actor-a",
        "entity_type": "character",
        "canonical_name": "测试角色甲",
        "aliases": ["角色甲", "甲号"],
    },
    {
        "entity_id": "character:actor-b",
        "entity_type": "character",
        "canonical_name": "测试角色丙",
        "aliases": ["角色乙", "乙号"],
    },
)
LOCATIONS = (
    {
        "entity_id": "location:testcity-east-gate",
        "entity_type": "location",
        "canonical_name": "测试城东门",
        "aliases": ["东门"],
    },
    {
        "entity_id": "location:linhe-town",
        "entity_type": "location",
        "canonical_name": "临河镇",
        "aliases": ["河镇"],
    },
    {
        "entity_id": "location:skyrail-platform",
        "entity_type": "location",
        "canonical_name": "天轨站台",
        "aliases": ["站台"],
    },
    {
        "entity_id": "location:blackstone-market",
        "entity_type": "location",
        "canonical_name": "黑石集市",
        "aliases": ["黑市"],
    },
)
ITEMS = (
    {
        "entity_id": "item:scarlet-token",
        "entity_type": "item",
        "canonical_name": "赤金令",
        "aliases": ["赤令"],
    },
    {
        "entity_id": "item:bronze-key",
        "entity_type": "item",
        "canonical_name": "青铜钥匙",
        "aliases": ["铜钥"],
    },
    {
        "entity_id": "item:rail-ticket",
        "entity_type": "item",
        "canonical_name": "跨层车票",
        "aliases": ["车票"],
    },
    {
        "entity_id": "item:ember-stone",
        "entity_type": "item",
        "canonical_name": "余烬石",
        "aliases": ["火石"],
    },
)
RELATIONS = (
    ("trust", "信任值"),
    ("hostility", "敌意值"),
    ("debt", "人情债"),
    ("loyalty", "忠诚度"),
)


def _line(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _quote_evidence(sentence: str) -> dict[str, Any]:
    return {
        "quote": sentence,
        "start": 0,
        "end": len(sentence),
        "sha256": hashlib.sha256(sentence.encode("utf-8")).hexdigest(),
    }


def _proposal_block(proposal: Mapping[str, Any]) -> str:
    return f"{OPEN_MARKER}\n{_line(proposal)}\n{CLOSE_MARKER}"


def _surface(entity: Mapping[str, Any], local_index: int) -> tuple[str, bool]:
    aliases = [str(alias) for alias in entity.get("aliases") or []]
    if aliases and local_index % 2:
        return aliases[(local_index // 2) % len(aliases)], True
    return str(entity["canonical_name"]), False


def _resolution(
    *,
    candidate_id: str,
    mention_field: str,
    mention: str,
    entity: Mapping[str, Any],
    alias: bool,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "mention_field": mention_field,
        "mention": mention,
        "entity_id": str(entity["entity_id"]),
        "alias": alias,
    }


def _accepted_case(
    category: str,
    global_index: int,
    local_index: int,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    candidate_id = f"delta-{global_index:04d}-accepted"
    actor = CHARACTERS[0]
    target = CHARACTERS[1]
    actor_mention, actor_alias = _surface(actor, local_index)
    target_mention, target_alias = _surface(target, local_index + 1)
    catalog: list[dict[str, Any]] = [dict(entity) for entity in CHARACTERS]
    resolutions: list[dict[str, Any]] = []

    if category == "location":
        location = LOCATIONS[(local_index - 1) % len(LOCATIONS)]
        location_mention, location_alias = _surface(location, local_index + 2)
        catalog.append(dict(location))
        sentence = (
            f"连续性更新：{actor_mention}抵达{location_mention}，"
            "当前位置随之更新。"
        )
        event = {
            "event_type": "movement",
            "action": "arrive",
            "actor_mention": actor_mention,
            "to_location_mention": location_mention,
            "scope": "current",
        }
        signature = {
            "event_type": "movement",
            "scope": "current",
            "action": "arrive",
            "actor_entity_id": actor["entity_id"],
            "to_location_entity_id": location["entity_id"],
        }
        terms = [actor_mention, location_mention]
        resolutions.extend(
            [
                _resolution(
                    candidate_id=candidate_id,
                    mention_field="actor_mention",
                    mention=actor_mention,
                    entity=actor,
                    alias=actor_alias,
                ),
                _resolution(
                    candidate_id=candidate_id,
                    mention_field="to_location_mention",
                    mention=location_mention,
                    entity=location,
                    alias=location_alias,
                ),
            ]
        )
    elif category == "inventory":
        item = ITEMS[(local_index - 1) % len(ITEMS)]
        item_mention, item_alias = _surface(item, local_index + 2)
        quantity = local_index % 5 + 1
        catalog.append(dict(item))
        sentence = f"连续性更新：{actor_mention}获得{quantity}枚{item_mention}。"
        event = {
            "event_type": "inventory",
            "action": "acquire",
            "item_mention": item_mention,
            "to_owner_mention": actor_mention,
            "quantity": quantity,
            "unique": False,
            "scope": "current",
        }
        signature = {
            "event_type": "inventory",
            "scope": "current",
            "action": "acquire",
            "item_entity_id": item["entity_id"],
            "to_owner_entity_id": actor["entity_id"],
            "quantity": float(quantity),
            "unique": False,
        }
        terms = [actor_mention, item_mention, str(quantity)]
        resolutions.extend(
            [
                _resolution(
                    candidate_id=candidate_id,
                    mention_field="item_mention",
                    mention=item_mention,
                    entity=item,
                    alias=item_alias,
                ),
                _resolution(
                    candidate_id=candidate_id,
                    mention_field="to_owner_mention",
                    mention=actor_mention,
                    entity=actor,
                    alias=actor_alias,
                ),
            ]
        )
    elif category == "story_time":
        day = local_index % 23 + 2
        watches = ("卯时", "午时", "酉时", "子时")
        value = f"第{day}日{watches[(local_index - 1) % len(watches)]}"
        sentence = f"连续性更新：故事时间推进到{value}。"
        event = {
            "event_type": "time",
            "field": "story_clock",
            "value": value,
            "story_time": value,
            "scope": "current",
        }
        signature = {
            "event_type": "time",
            "scope": "current",
            "field": "story_clock",
            "value": value,
            "story_time": value,
            "narrative_mode": "linear",
        }
        terms = [value]
    elif category == "relation":
        dimension, label = RELATIONS[(local_index - 1) % len(RELATIONS)]
        value = 40 + local_index
        sentence = (
            f"连续性更新：{actor_mention}与{target_mention}的"
            f"{label}调整为{value}。"
        )
        event = {
            "event_type": "relation",
            "source_mention": actor_mention,
            "target_mention": target_mention,
            "dimension": dimension,
            "value": value,
            "scope": "current",
        }
        signature = {
            "event_type": "relation",
            "scope": "current",
            "source_entity_id": actor["entity_id"],
            "target_entity_id": target["entity_id"],
            "dimension": dimension,
            "value": value,
        }
        terms = [actor_mention, target_mention, label, str(value)]
        resolutions.extend(
            [
                _resolution(
                    candidate_id=candidate_id,
                    mention_field="source_mention",
                    mention=actor_mention,
                    entity=actor,
                    alias=actor_alias,
                ),
                _resolution(
                    candidate_id=candidate_id,
                    mention_field="target_mention",
                    mention=target_mention,
                    entity=target,
                    alias=target_alias,
                ),
            ]
        )
    else:
        raise ValueError(f"unsupported category: {category}")

    proposal = {
        "proposal_version": 1,
        "candidate_id": candidate_id,
        "event": event,
        "evidence": _quote_evidence(sentence),
        "evidence_terms": terms,
    }
    expected = {
        "candidate_id": candidate_id,
        "event_signature": signature,
    }
    return sentence, proposal, expected, catalog, resolutions


def _dangerous_case(
    category: str,
    global_index: int,
    local_index: int,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    candidate_id = f"delta-{global_index:04d}-danger"
    actor = CHARACTERS[0]
    actor_mention, actor_alias = _surface(actor, local_index)
    catalog: list[dict[str, Any]] = [dict(entity) for entity in CHARACTERS]
    resolutions: list[dict[str, Any]] = []

    if category == "location":
        location = LOCATIONS[(local_index - 1) % len(LOCATIONS)]
        location_mention, location_alias = _surface(location, local_index + 2)
        catalog.append(dict(location))
        sentence = (
            f"连续性候选：{actor_mention}离开{location_mention}，"
            f"并继续把当前位置设为{location_mention}。"
        )
        event = {
            "event_type": "movement",
            "action": "leave",
            "actor_mention": actor_mention,
            "location_mention": location_mention,
            "scope": "current",
        }
        terms = [actor_mention, location_mention]
        expected = {
            "candidate_id": candidate_id,
            "validator_stage": "continuity",
            "code": "LEAVE_CANNOT_SET_LOCATION",
        }
        resolutions.extend(
            [
                _resolution(
                    candidate_id=candidate_id,
                    mention_field="actor_mention",
                    mention=actor_mention,
                    entity=actor,
                    alias=actor_alias,
                ),
                _resolution(
                    candidate_id=candidate_id,
                    mention_field="location_mention",
                    mention=location_mention,
                    entity=location,
                    alias=location_alias,
                ),
            ]
        )
    elif category == "inventory":
        item = ITEMS[(local_index - 1) % len(ITEMS)]
        item_mention, item_alias = _surface(item, local_index + 2)
        quantity = local_index % 4 + 1
        catalog.append(dict(item))
        sentence = (
            f"连续性候选：{actor_mention}把{quantity}枚{item_mention}"
            "从自己手中转交给自己。"
        )
        event = {
            "event_type": "inventory",
            "action": "transfer",
            "item_mention": item_mention,
            "from_owner_mention": actor_mention,
            "to_owner_mention": actor_mention,
            "quantity": quantity,
            "unique": False,
            "scope": "current",
        }
        terms = [actor_mention, item_mention, str(quantity)]
        expected = {
            "candidate_id": candidate_id,
            "validator_stage": "continuity",
            "code": "INVENTORY_TRANSFER_SAME_OWNER",
        }
        resolutions.extend(
            [
                _resolution(
                    candidate_id=candidate_id,
                    mention_field="item_mention",
                    mention=item_mention,
                    entity=item,
                    alias=item_alias,
                ),
                _resolution(
                    candidate_id=candidate_id,
                    mention_field="from_owner_mention",
                    mention=actor_mention,
                    entity=actor,
                    alias=actor_alias,
                ),
                _resolution(
                    candidate_id=candidate_id,
                    mention_field="to_owner_mention",
                    mention=actor_mention,
                    entity=actor,
                    alias=actor_alias,
                ),
            ]
        )
    elif category == "story_time":
        day = local_index % 19 + 3
        value = f"第{day}日未时"
        sentence = (
            f"连续性候选：故事时间更新为{value}，"
            "却把叙事模式写成未支持的逆向模式。"
        )
        event = {
            "event_type": "time",
            "field": "story_clock",
            "value": value,
            "story_time": value,
            "narrative_mode": "reverse",
            "scope": "current",
        }
        terms = [value, "逆向模式"]
        expected = {
            "candidate_id": candidate_id,
            "validator_stage": "continuity",
            "code": "INVALID_NARRATIVE_MODE",
        }
    elif category == "relation":
        sentence = (
            f"连续性候选：{actor_mention}与{actor_mention}"
            "的师徒关系调整为成立。"
        )
        event = {
            "event_type": "relation",
            "source_mention": actor_mention,
            "target_mention": actor_mention,
            "dimension": "mentor",
            "value": True,
            "scope": "current",
        }
        terms = [actor_mention, "师徒关系", "成立"]
        expected = {
            "candidate_id": candidate_id,
            "validator_stage": "semantic",
            "code": "SELF_RELATION_NOT_ALLOWED",
        }
        resolutions.extend(
            [
                _resolution(
                    candidate_id=candidate_id,
                    mention_field="source_mention",
                    mention=actor_mention,
                    entity=actor,
                    alias=actor_alias,
                ),
                _resolution(
                    candidate_id=candidate_id,
                    mention_field="target_mention",
                    mention=actor_mention,
                    entity=actor,
                    alias=actor_alias,
                ),
            ]
        )
    else:
        raise ValueError(f"unsupported category: {category}")

    proposal = {
        "proposal_version": 1,
        "candidate_id": candidate_id,
        "event": event,
        "evidence": _quote_evidence(sentence),
        "evidence_terms": terms,
    }
    return sentence, proposal, expected, catalog, resolutions


def generate_annotations(count: int = 240) -> list[dict[str, Any]]:
    if count < 200:
        raise ValueError("versioned annotation manifest must contain at least 200 cases")
    if count % len(CATEGORIES):
        raise ValueError("annotation count must be divisible by four categories")
    cases_per_category = count // len(CATEGORIES)
    if cases_per_category < 50:
        raise ValueError("each category requires accepted, dangerous, and zero cases")
    accepted_per_category = cases_per_category - 20
    records: list[dict[str, Any]] = []
    global_index = 0
    for category in CATEGORIES:
        for local_index in range(1, cases_per_category + 1):
            global_index += 1
            if local_index <= accepted_per_category:
                case_kind = "accepted_delta"
                sentence, proposal, expected, catalog, resolutions = _accepted_case(
                    category,
                    global_index,
                    local_index,
                )
                assistant_text = f"{sentence}\n\n{_proposal_block(proposal)}"
                expected_accepted = [expected]
                expected_quarantine: list[dict[str, Any]] = []
            elif local_index <= accepted_per_category + 10:
                case_kind = "dangerous_delta"
                sentence, proposal, expected, catalog, resolutions = _dangerous_case(
                    category,
                    global_index,
                    local_index,
                )
                assistant_text = f"{sentence}\n\n{_proposal_block(proposal)}"
                expected_accepted = []
                expected_quarantine = [expected]
            else:
                case_kind = "zero_delta"
                catalog = [dict(entity) for entity in CHARACTERS]
                resolutions = []
                assistant_text = (
                    "这段只描写风声掠过站台与人物的犹豫，"
                    "没有形成角色状态、位置、库存、时间或关系变更。"
                )
                expected_accepted = []
                expected_quarantine = []
            records.append(
                {
                    "manifest_version": 1,
                    "case_id": f"longform-{global_index:04d}",
                    "genre": GENRES[(global_index - 1) % len(GENRES)],
                    "task": TASKS[(global_index - 1) % len(TASKS)],
                    "category": category,
                    "case_kind": case_kind,
                    "artifact_stage": "final",
                    "branch_id": "main",
                    "chapter_no": global_index + 10,
                    "scene_index": global_index % 4,
                    "assistant_text": assistant_text,
                    "entity_catalog": catalog,
                    "expected_accepted": expected_accepted,
                    "expected_quarantine": expected_quarantine,
                    "expected_resolutions": resolutions,
                }
            )
    return records


def generate_chapters(count: int = 500) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for chapter_no in range(1, count + 1):
        volume = (chapter_no - 1) // 100 + 1
        arc = (chapter_no - 1) // 25 + 1
        marker = f"CHAPTERMARKER{chapter_no:04d}"
        records.append(
            {
                "fixture_version": 1,
                "chapter_no": chapter_no,
                "volume_id": f"volume-{volume:02d}",
                "arc_id": f"arc-{arc:03d}",
                "path": f"正文/第{chapter_no:04d}章.md",
                "text": (
                    f"# 第{chapter_no}章\n\n"
                    f"{marker}：主角在第{chapter_no}章的当前位置为区域{chapter_no % 17}，"
                    f"当前状态编号为{chapter_no % 23}。\n\n"
                    f"活跃剧情债务 DEBT{chapter_no:04d} 的兑现窗口是"
                    f"第{chapter_no + 3}章，持有道具 ITEM{chapter_no % 31:02d}。"
                ),
            }
        )
    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(_line(record) + "\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic long-form benchmark fixtures."
    )
    parser.add_argument("--annotation-count", type=int, default=240)
    parser.add_argument("--chapter-count", type=int, default=500)
    args = parser.parse_args()
    write_jsonl(ANNOTATION_PATH, generate_annotations(args.annotation_count))
    write_jsonl(CHAPTER_PATH, generate_chapters(args.chapter_count))
    print(
        _line(
            {
                "annotations": str(ANNOTATION_PATH.relative_to(ROOT)),
                "annotation_count": args.annotation_count,
                "chapters": str(CHAPTER_PATH.relative_to(ROOT)),
                "chapter_count": args.chapter_count,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
