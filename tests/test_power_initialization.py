from __future__ import annotations

import copy
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from plot_init import PlotInitError, PlotInitService, proposal_to_lifecycle_package
from plot_init.inventory import extract_claims
from plot_init.normalized import export_normalized_bundle, parse_normalized_export
from plot_init.remote_model import _validate_claims
from power_system import (
    adapter_registry,
    build_power_package,
    normalize_power_package,
    power_sufficiency,
)
from tests.test_plot_init import complete_seed


def cultivation_seed() -> dict[str, object]:
    seed = complete_seed()
    seed.update(
        {
            "power_profile": "cultivation",
            "power_systems": [
                {
                    "namespace": "project.cultivation",
                    "name": "玄门修行",
                    "profile": "cultivation",
                    "social_consequences": ["境界决定宗门权限与可进入区域"],
                }
            ],
            "progression_tracks": [
                {
                    "namespace": "project.cultivation.realm",
                    "name": "修为",
                    "track_kind": "ordered_rank",
                }
            ],
            "rank_nodes": [
                {
                    "name": "练气",
                    "track_namespace": "project.cultivation.realm",
                    "order": 1,
                },
                {
                    "name": "筑基",
                    "track_namespace": "project.cultivation.realm",
                    "order": 2,
                },
            ],
            "rank_edges": [
                {
                    "track_namespace": "project.cultivation.realm",
                    "from_node_ids": ["练气"],
                    "to_node_id": "筑基",
                    "prerequisites": {"foundation": "练气圆满"},
                    "resource_costs": [{"resource_name": "灵力", "amount": 100}],
                    "failure_outcomes": ["根基受损"],
                }
            ],
            "ability_definitions": [
                {
                    "name": "御剑术",
                    "ability_kind": "active",
                    "source_bindings": ["玄门剑诀"],
                    "effects": ["御剑移动与攻击"],
                    "costs": ["持续消耗灵力"],
                    "conditions": ["练气"],
                    "limits": ["必须持有飞剑"],
                    "counters": ["禁空阵法"],
                }
            ],
            "resource_definitions": [
                {
                    "name": "灵力",
                    "resource_kind": "stock",
                    "acquisition": ["吐纳"],
                    "consumption": ["施术"],
                    "recovery": ["打坐"],
                }
            ],
            "actor_power_bootstrap": [
                {
                    "actor_name": "测试角色甲",
                    "progression_states": [
                        {
                            "track_namespace": "project.cultivation.realm",
                            "rank_name": "练气",
                        }
                    ],
                    "ability_ownerships": [
                        {
                            "ability_name": "御剑术",
                            "level": 1,
                            "costs": ["持续消耗灵力"],
                            "limits": ["必须持有飞剑"],
                        }
                    ],
                    "resources": [{"resource_name": "灵力", "amount": 9}],
                    "statuses": [],
                    "bindings": [],
                    "qualifications": [],
                    "observed_capabilities": [],
                }
            ],
        }
    )
    return seed


def text_document(text: str) -> dict[str, object]:
    return {
        "source_id": "src-synthetic-shadow",
        "source_version_id": "srcv-synthetic-shadow-v1",
        "path": "synthetic-shadow.md",
        "real_path": "synthetic-shadow.md",
        "normalized_real_path": "synthetic-shadow.md",
        "content_hash": "synthetic-shadow-content-hash",
        "parse_status": "parsed",
        "ingest_policy": "include",
        "source_role": "setting",
        "authority_tier": "T2",
        "artifact_stage": "accepted",
        "scope_policy": "preserve_unknown",
        "branch_id": "main",
        "classification_confidence": 0.99,
        "_text": text,
    }


class PowerInitializationTestCase(unittest.TestCase):
    def test_plain_institutional_language_does_not_create_abilities(self) -> None:
        text = "\n".join(
            [
                "# 城市制度",
                "测试城议会拥有调配跨区列车容量的权力。",
                "交通署拥有线路审批权限。",
                "旧城区拥有三条备用通道。",
                "该制度掌握全城粮票的分配情况。",
                "委员会获得了更大的财政自主权。",
                "管理局学会总结历年事故情况。",
                "单线列车容量限制：每刻钟只能通过一列。",
                "跨层通行成本：需提交双重凭证。",
                "制度来源：旧王朝交通法。",
                "权力边界：不得越过城防军指挥链。",
                "预算反制：由审计院冻结下一季度拨款。",
                "审批冷却：材料退回后七日内不得重提。",
                "# 能力",
                "能力限制：这里只是尚未命名的占位说明。",
                "技能：技能",
                "测试角色甲拥有技能能力。",
                "能力《能力》的反制：尚未填写。",
            ]
        )
        claims = extract_claims(text_document(text))
        power_claims = [
            claim
            for claim in claims
            if str(claim["predicate"]).startswith("ability.")
        ]
        self.assertEqual([], power_claims)

        package = build_power_package({}, claims, mode="ingest")
        self.assertEqual([], package["ability_definitions"])
        self.assertEqual([], package["actor_power_bootstrap"])

    def test_bare_ability_labels_are_placeholder_only(self) -> None:
        report = power_sufficiency(
            {
                "power_systems": [{"profile": "technology"}],
                "progression_tracks": [
                    {"track_kind": "open_ended"},
                ],
                "rank_nodes": [],
                "rank_edges": [],
                "resource_definitions": [],
                "ability_definitions": [
                    {
                        "name": "能力",
                        "evidence_claim_ids": ["claim-placeholder"],
                        "source_bindings": ["未命名来源"],
                        "costs": ["未命名代价"],
                        "limits": ["未命名限制"],
                        "counters": ["未命名反制"],
                    }
                ],
            },
            mode="new",
        )
        self.assertFalse(report["checks"]["no_placeholder_only"])
        self.assertFalse(report["sufficient"])

        report_without_evidence = power_sufficiency(
            {
                "power_systems": [{"profile": "technology"}],
                "progression_tracks": [
                    {"track_kind": "open_ended"},
                ],
                "rank_nodes": [],
                "rank_edges": [],
                "resource_definitions": [],
                "ability_definitions": [
                    {
                        "name": "能力",
                        "source_bindings": ["未命名来源"],
                        "costs": ["未命名代价"],
                        "limits": ["未命名限制"],
                        "counters": ["未命名反制"],
                    }
                ],
            },
            mode="new",
        )
        self.assertFalse(
            report_without_evidence["checks"]["no_placeholder_only"]
        )
        self.assertFalse(report_without_evidence["sufficient"])

    def test_quoted_documents_remain_inventory_claims(self) -> None:
        text = "\n".join(
            [
                "测试角色甲拥有《边境史书籍》。",
                "苏晚获得《列车检修手册》。",
                "叶舟拥有《测试城交通法汇编》。",
                "林夏获得《旧王朝法规》。",
                "陈默拥有《禁区事故档案》。",
            ]
        )
        claims = extract_claims(text_document(text))

        self.assertEqual(
            [],
            [
                claim
                for claim in claims
                if claim["predicate"] == "ability.owns"
            ],
        )
        self.assertEqual(
            [
                ("测试角色甲", "《边境史书籍》"),
                ("苏晚", "《列车检修手册》"),
                ("叶舟", "《测试城交通法汇编》"),
                ("林夏", "《旧王朝法规》"),
                ("陈默", "《禁区事故档案》"),
            ],
            [
                (claim["subject"], claim["object_or_value"])
                for claim in claims
                if claim["predicate"] == "inventory.holds"
            ],
        )

    def test_explicit_quoted_ability_labels_keep_weak_verb_recall(self) -> None:
        text = "\n".join(
            [
                "测试角色甲拥有技能《焰矢》。",
                "苏晚获得法术《火球术》。",
                "叶舟学会《御剑术》，此术可御剑飞行。",
                "林夏掌握《踏雪无痕》。",
                "陈默领悟《踏雪无痕》。",
                "白璃习得《踏雪无痕》。",
                "赵澈修成《踏雪无痕》。",
                "唐宁练成《踏雪无痕》。",
            ]
        )
        claims = extract_claims(text_document(text))

        self.assertEqual(
            [
                ("测试角色甲", "焰矢"),
                ("苏晚", "火球术"),
                ("叶舟", "御剑术"),
                ("林夏", "踏雪无痕"),
                ("陈默", "踏雪无痕"),
                ("白璃", "踏雪无痕"),
                ("赵澈", "踏雪无痕"),
                ("唐宁", "踏雪无痕"),
            ],
            [
                (claim["subject"], claim["object_or_value"])
                for claim in claims
                if claim["predicate"] == "ability.owns"
            ],
        )
        self.assertEqual(
            [],
            [
                claim
                for claim in claims
                if claim["predicate"] == "inventory.holds"
            ],
        )

    def test_explicit_and_controlled_ability_grammar_keeps_recall(self) -> None:
        text = "\n".join(
            [
                "# 角色能力",
                "测试角色甲掌握御剑术。",
                "苏晚学会《敛息诀》。",
                "叶舟觉醒空间切割。",
                "林夏解锁技能影步。",
                "陈默拥有法术火球术。",
                "# 白璃",
                "技能：霜刃",
                "御剑术限制：必须持有飞剑。",
                "《敛息诀》代价：持续消耗灵力。",
                "能力《空间切割》的反制：封闭空间。",
                "技能影步冷却：十息。",
            ]
        )
        claims = extract_claims(text_document(text))
        ownerships = [
            (claim["subject"], claim["object_or_value"])
            for claim in claims
            if claim["predicate"] == "ability.owns"
        ]
        self.assertEqual(
            [
                ("测试角色甲", "御剑术"),
                ("苏晚", "敛息诀"),
                ("叶舟", "空间切割"),
                ("林夏", "影步"),
                ("陈默", "火球术"),
                ("白璃", "霜刃"),
            ],
            ownerships,
        )
        rules = {
            (
                claim["subject"],
                claim["predicate"],
                claim["object_or_value"],
            )
            for claim in claims
            if claim["predicate"]
            in {
                "ability.limit",
                "ability.cost",
                "ability.counter",
                "ability.cooldown",
            }
        }
        self.assertEqual(
            {
                ("御剑术", "ability.limit", "必须持有飞剑。"),
                ("敛息诀", "ability.cost", "持续消耗灵力。"),
                ("空间切割", "ability.counter", "封闭空间。"),
                ("影步", "ability.cooldown", "十息。"),
            },
            rules,
        )

    def test_shadow_style_corpus_keeps_power_extraction_bounded(self) -> None:
        negative_templates = [
            "第{index}区议会拥有调配列车容量的权力。",
            "第{index}区交通署拥有线路审批权限。",
            "第{index}区容量限制：每刻钟仅通行一列。",
            "第{index}区制度来源：旧交通法第九条。",
            "第{index}区治理成本：需提交双重凭证。",
            "第{index}区权力边界：不得调动城防军。",
        ]
        lines = ["# 匿名作品合成影子样本"]
        for index in range(1, 31):
            lines.extend(
                template.format(index=index)
                for template in negative_templates
            )
        lines.extend(
            [
                "测试角色甲掌握御剑术。",
                "苏晚学会《敛息诀》。",
                "叶舟觉醒空间切割。",
                "御剑术限制：必须持有飞剑。",
                "《敛息诀》代价：持续消耗灵力。",
            ]
        )

        claims = extract_claims(text_document("\n".join(lines)))
        ownerships = [
            claim
            for claim in claims
            if claim["predicate"] == "ability.owns"
        ]
        rules = [
            claim
            for claim in claims
            if claim["predicate"].startswith("ability.")
            and claim["predicate"] != "ability.owns"
        ]
        package = build_power_package({}, claims, mode="ingest")

        self.assertEqual(3, len(ownerships))
        self.assertEqual(2, len(rules))
        self.assertLessEqual(len(package["ability_definitions"]), 3)
        self.assertLessEqual(len(package["actor_power_bootstrap"]), 3)

    def test_package_import_and_all_twelve_adapters(self) -> None:
        imported = importlib.import_module("scripts.plot_init")
        self.assertTrue(hasattr(imported, "PlotInitService"))
        self.assertEqual(
            {
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
            },
            set(adapter_registry().profiles()),
        )

    def test_mundane_profile_never_generates_rank_chain(self) -> None:
        package = build_power_package(
            {
                "power_profile": "mundane",
                "power_systems": [
                    {
                        "namespace": "project.mundane",
                        "name": "现实技能",
                        "profile": "mundane",
                        "no_resource_pool": True,
                    }
                ],
            },
            mode="new",
        )
        self.assertEqual("mundane", package["power_systems"][0]["profile"])
        self.assertEqual([], package["progression_tracks"])
        self.assertEqual([], package["rank_nodes"])
        self.assertEqual([], package["rank_edges"])

    def test_explicit_v2_inserts_power_question_and_mundane_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            project.mkdir()
            service = PlotInitService(workspace)
            started = service.start(
                project_root=project,
                mode="new",
                interaction_profile="deep",
                seed=complete_seed(),
                bundle_schema_version="plot-rag-init/v2",
                idempotency_key="start-v2-question",
            )
            self.assertEqual("POWER_CAUSAL_KERNEL", started["stage"])
            self.assertEqual(
                "power-causal-kernel",
                started["current_questions"][0]["question_id"],
            )
            answered = service.answer(
                started["session_id"],
                {"power-causal-kernel": {"option_id": "power-mundane"}},
                expected_session_revision=started["session_revision"],
                idempotency_key="answer-v2-mundane",
            )
            self.assertEqual("READY_TO_PROPOSE", answered["status"])
            self.assertEqual("plot-rag-init/v2", answered["bundle"]["schema_version"])
            self.assertEqual([], answered["bundle"]["rank_nodes"])
            self.assertEqual(
                "not_applicable",
                answered["bundle"]["validation"]["power_model_status"],
            )

    def test_v2_bundle_and_lifecycle_are_deterministic_and_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            project.mkdir()
            service = PlotInitService(workspace)
            first = service.dry_run(
                project_root=project,
                mode="new",
                interaction_profile="deep",
                seed=cultivation_seed(),
                bundle_schema_version="plot-rag-init/v2",
            )
            second = service.dry_run(
                project_root=project,
                mode="new",
                interaction_profile="deep",
                seed=cultivation_seed(),
                bundle_schema_version="plot-rag-init/v2",
            )
            self.assertEqual(
                first["bundle"]["bundle_hash"],
                second["bundle"]["bundle_hash"],
            )
            self.assertTrue(
                first["bundle"]["validation"]["power_sufficiency"]["sufficient"]
            )
            started = service.start(
                project_root=project,
                mode="new",
                interaction_profile="deep",
                seed=cultivation_seed(),
                bundle_schema_version="plot-rag-init/v2",
                idempotency_key="start-v2-typed",
            )
            proposed = service.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="propose-v2-typed",
            )
            lifecycle = proposal_to_lifecycle_package(proposed["proposal"])
            event_types = {item["event_type"] for item in lifecycle["events"]}
            self.assertTrue({"progression", "resource", "ability"} <= event_types)
            self.assertNotIn("power_spec", event_types)
            self.assertTrue(lifecycle["requires_power_spec_acceptance"])
            self.assertEqual(
                "power_spec_change",
                lifecycle["power_spec_package"]["proposal_kind"],
            )
            self.assertTrue(
                all(
                    event["event_type"] == "power_spec"
                    for event in lifecycle["power_spec_package"]["events"]
                )
            )

    def test_v2_status_qualification_and_conversion_definitions_reach_power_spec(
        self,
    ) -> None:
        seed = copy.deepcopy(cultivation_seed())
        seed["resource_definitions"].append(
            {
                "name": "剑意",
                "resource_kind": "stock",
                "acquisition": ["完成剑道领悟"],
                "consumption": ["强化剑术"],
                "recovery": ["复盘战斗"],
            }
        )
        seed["status_definitions"] = [
            {
                "name": "灵力灼伤",
                "status_kind": "debuff",
                "stack_policy": "stack",
                "max_stacks": 3,
                "effects": ["施术成本提高"],
                "removal_conditions": ["静养一个故事时段"],
            }
        ]
        seed["qualification_definitions"] = [
            {
                "name": "御剑许可",
                "qualification_kind": "permission",
                "grant_sources": ["玄门执事授予"],
                "consumption_rules": [],
                "expiry_rules": ["退出宗门后失效"],
                "prerequisites": ["通过御剑考核"],
                "effects": ["允许在宗门空域御剑"],
                "max_quantity": 1,
            }
        ]
        seed["conversion_rules"] = [
            {
                "source_resource": "灵力",
                "target_resource": "剑意",
                "ratio": 0.5,
                "fixed_cost": 1,
                "loss_ratio": 0.1,
                "rounding": "floor",
                "conditions": ["持有玄门剑诀"],
            }
        ]
        seed["actor_power_bootstrap"][0]["statuses"] = [
            {
                "status_name": "灵力灼伤",
                "stacks": 1,
            }
        ]
        seed["actor_power_bootstrap"][0]["qualifications"] = [
            {
                "qualification_name": "御剑许可",
                "quantity": 1,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            service = PlotInitService(temporary)
            started = service.start(
                project_root=Path(temporary) / "novel",
                mode="new",
                interaction_profile="deep",
                seed=seed,
                bundle_schema_version="plot-rag-init/v2",
                idempotency_key="start-v2-status-conversion",
            )
            bundle = started["bundle"]
            self.assertEqual(1, len(bundle["status_definitions"]))
            self.assertEqual(1, len(bundle["qualification_definitions"]))
            self.assertEqual(1, len(bundle["conversion_rules"]))
            status_id = bundle["status_definitions"][0]["status_id"]
            self.assertEqual(
                status_id,
                bundle["actor_power_bootstrap"][0]["statuses"][0]["status_id"],
            )
            qualification_id = bundle["qualification_definitions"][0][
                "qualification_id"
            ]
            self.assertEqual(
                qualification_id,
                bundle["actor_power_bootstrap"][0]["qualifications"][0][
                    "qualification_id"
                ],
            )
            conversion = bundle["conversion_rules"][0]
            self.assertEqual(0.5, conversion["ratio"])
            self.assertNotEqual(
                conversion["source_resource_id"],
                conversion["target_resource_id"],
            )
            proposed = service.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="propose-v2-status-conversion",
            )
            lifecycle = proposal_to_lifecycle_package(proposed["proposal"])
            spec_types = {
                event["spec_type"]
                for event in lifecycle["power_spec_package"]["events"]
            }
            self.assertIn("status_definition", spec_types)
            self.assertIn("qualification_definition", spec_types)
            self.assertIn("conversion_rule", spec_types)
            self.assertIn(
                "status_effect",
                {event["event_type"] for event in lifecycle["events"]},
            )
            self.assertIn(
                "qualification",
                {event["event_type"] for event in lifecycle["events"]},
            )

    def test_local_ability_claims_survive_bundle_and_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            setting = project / "设定集"
            setting.mkdir(parents=True)
            (setting / "力量体系.md").write_text(
                "# 测试角色甲\n"
                "状态：已定稿\n"
                "力量体系：修仙\n"
                "测试角色甲掌握御剑术\n"
                "御剑术来源：玄门剑诀\n"
                "御剑术代价：消耗三成灵力\n"
                "御剑术限制：必须持有飞剑\n"
                "御剑术反制：禁空阵法\n"
                "御剑术冷却：一刻钟\n",
                encoding="utf-8",
            )
            service = PlotInitService(workspace)
            started = service.start(
                project_root=project,
                mode="ingest",
                bundle_schema_version="auto",
                idempotency_key="start-ability-ingest",
            )
            self.assertEqual("plot-rag-init/v2", started["bundle"]["schema_version"])
            ability = started["bundle"]["ability_definitions"][0]
            self.assertEqual(["玄门剑诀"], ability["source_bindings"])
            self.assertEqual(["消耗三成灵力"], ability["costs"])
            self.assertEqual(["必须持有飞剑"], ability["limits"])
            self.assertEqual(["禁空阵法"], ability["counters"])
            self.assertEqual("一刻钟", ability["cooldown"])
            proposed = service.propose(
                started["session_id"],
                expected_session_revision=started["session_revision"],
                idempotency_key="propose-ability-ingest",
            )
            lifecycle = proposal_to_lifecycle_package(proposed["proposal"])
            ability_events = [
                item
                for item in lifecycle["events"]
                if item["event_type"] == "ability"
            ]
            self.assertEqual(1, len(ability_events))
            state = ability_events[0]["state"]
            self.assertEqual(["玄门剑诀"], state["source_bindings"])
            self.assertEqual(["消耗三成灵力"], state["costs"])
            self.assertEqual(["必须持有飞剑"], state["limits"])
            self.assertEqual(["禁空阵法"], state["counters"])
            self.assertEqual("一刻钟", state["cooldown"])

    def test_local_compact_power_notation_builds_complete_typed_claims(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"
            setting = project / "设定集"
            setting.mkdir(parents=True)
            (setting / "力量体系.md").write_text(
                "# 艾琳\n"
                "状态：已定稿\n"
                "力量体系：魔法\n"
                "体系名称：学院法术体系\n"
                "成长轨：环阶|ordered_rank\n"
                "境界节点：环阶|一环\n"
                "境界节点：环阶|二环\n"
                "晋升边：环阶|一环->二环\n"
                "资源定义：法力\n"
                "资源定义：专注\n"
                "当前境界：环阶|一环\n"
                "当前资源：法力=12\n"
                "状态定义：沉默\n"
                "当前状态：沉默=1\n"
                "资格定义：二环许可\n"
                "当前资格：二环许可=1\n"
                "力量绑定：item|学院法杖|火球术\n"
                "转换规则：法力->专注=0.5\n"
                "艾琳掌握火球术\n",
                encoding="utf-8",
            )
            service = PlotInitService(workspace)
            started = service.start(
                project_root=project,
                mode="ingest",
                bundle_schema_version="auto",
                idempotency_key="start-compact-power-ingest",
            )
            bundle = started["bundle"]
            self.assertEqual("plot-rag-init/v2", bundle["schema_version"])
            self.assertEqual("学院法术体系", bundle["power_systems"][0]["name"])
            self.assertEqual(1, len(bundle["progression_tracks"]))
            self.assertEqual(2, len(bundle["rank_nodes"]))
            self.assertEqual(1, len(bundle["rank_edges"]))
            self.assertEqual(2, len(bundle["resource_definitions"]))
            self.assertEqual(1, len(bundle["status_definitions"]))
            self.assertEqual(1, len(bundle["qualification_definitions"]))
            self.assertEqual(1, len(bundle["conversion_rules"]))
            actor = bundle["actor_power_bootstrap"][0]
            self.assertEqual(1, len(actor["progression_states"]))
            self.assertEqual(1, len(actor["resources"]))
            self.assertEqual(1, len(actor["statuses"]))
            self.assertEqual(1, len(actor["bindings"]))
            self.assertEqual(1, len(actor["qualifications"]))

    def test_remote_ability_claim_schema_reaches_power_model(self) -> None:
        source = (
            "测试角色甲掌握御剑术\n"
            "御剑术来源玄门剑诀\n"
            "御剑术代价消耗三成灵力\n"
            "御剑术限制必须持有飞剑\n"
        )
        raw_claims = [
            {
                "subject": "测试角色甲",
                "predicate": "ability.owns",
                "object_or_value": "御剑术",
                "exact_evidence": "测试角色甲掌握御剑术",
                "confidence": 0.99,
            },
            {
                "subject": "御剑术",
                "predicate": "ability.source",
                "object_or_value": "玄门剑诀",
                "exact_evidence": "御剑术来源玄门剑诀",
                "confidence": 0.98,
            },
            {
                "subject": "御剑术",
                "predicate": "ability.cost",
                "object_or_value": "消耗三成灵力",
                "exact_evidence": "御剑术代价消耗三成灵力",
                "confidence": 0.98,
            },
            {
                "subject": "御剑术",
                "predicate": "ability.limit",
                "object_or_value": "必须持有飞剑",
                "exact_evidence": "御剑术限制必须持有飞剑",
                "confidence": 0.98,
            },
        ]
        validated = _validate_claims(
            {"claims": raw_claims},
            source_text=source,
        )
        claims = [
            {
                **item,
                "claim_id": f"claim-remote-{index}",
                "origin": "remote_ambiguity_proposal",
            }
            for index, item in enumerate(validated["claims"])
        ]
        package = build_power_package({}, claims, mode="ingest")
        ability = package["ability_definitions"][0]
        self.assertEqual(["玄门剑诀"], ability["source_bindings"])
        self.assertEqual(["消耗三成灵力"], ability["costs"])
        self.assertEqual(["必须持有飞剑"], ability["limits"])
        self.assertEqual(
            "御剑术",
            package["actor_power_bootstrap"][0]["ability_ownerships"][0][
                "native_value"
            ],
        )

    def test_all_power_claim_families_reach_typed_definitions_and_state(
        self,
    ) -> None:
        claims = [
            {
                "claim_id": "claim-system",
                "subject": "世界",
                "predicate": "power.system",
                "object_or_value": {
                    "name": "学院法术体系",
                    "profile": "magic",
                },
            },
            {
                "claim_id": "claim-track",
                "subject": "环阶",
                "predicate": "progression.track",
                "object_or_value": {
                    "name": "环阶",
                    "track_kind": "ordered_rank",
                },
            },
            {
                "claim_id": "claim-rank-one",
                "subject": "环阶",
                "predicate": "rank.node",
                "object_or_value": {"name": "一环", "track_name": "环阶"},
            },
            {
                "claim_id": "claim-rank-two",
                "subject": "环阶",
                "predicate": "rank.node",
                "object_or_value": {"name": "二环", "track_name": "环阶"},
            },
            {
                "claim_id": "claim-edge",
                "subject": "环阶",
                "predicate": "rank.edge",
                "object_or_value": {
                    "track_name": "环阶",
                    "from_node_ids": ["一环"],
                    "to_node_id": "二环",
                    "prerequisites": {"qualification": "二环许可"},
                    "failure_outcomes": ["晋升失败"],
                },
            },
            {
                "claim_id": "claim-ability",
                "subject": "艾琳",
                "predicate": "ability.owns",
                "object_or_value": "火球术",
            },
            {
                "claim_id": "claim-resource-mana",
                "subject": "法力",
                "predicate": "resource.definition",
                "object_or_value": {
                    "name": "法力",
                    "acquisition": ["冥想"],
                    "consumption": ["施法"],
                    "recovery": ["休息"],
                },
            },
            {
                "claim_id": "claim-resource-focus",
                "subject": "专注",
                "predicate": "resource.definition",
                "object_or_value": {
                    "name": "专注",
                    "acquisition": ["准备"],
                    "consumption": ["维持法术"],
                    "recovery": ["解除法术"],
                },
            },
            {
                "claim_id": "claim-status",
                "subject": "沉默",
                "predicate": "status.definition",
                "object_or_value": {
                    "name": "沉默",
                    "status_kind": "debuff",
                    "effects": ["不能吟唱"],
                },
            },
            {
                "claim_id": "claim-qualification",
                "subject": "二环许可",
                "predicate": "qualification.definition",
                "object_or_value": {
                    "name": "二环许可",
                    "qualification_kind": "permission",
                    "grant_sources": ["学院考核"],
                },
            },
            {
                "claim_id": "claim-counter",
                "subject": "沉默克制吟唱",
                "predicate": "counter.rule",
                "object_or_value": {
                    "name": "沉默克制吟唱",
                    "source_tags": ["沉默"],
                    "target_tags": ["吟唱"],
                },
            },
            {
                "claim_id": "claim-conversion",
                "subject": "冥想换算",
                "predicate": "conversion.rule",
                "object_or_value": {
                    "name": "法力转专注",
                    "source_resource": "法力",
                    "target_resource": "专注",
                    "ratio": 0.5,
                },
            },
            {
                "claim_id": "claim-progress-state",
                "subject": "艾琳",
                "predicate": "progression.state",
                "object_or_value": {
                    "track_name": "环阶",
                    "rank_name": "一环",
                },
            },
            {
                "claim_id": "claim-resource-state",
                "subject": "艾琳",
                "predicate": "resource.state",
                "object_or_value": {
                    "resource_name": "法力",
                    "amount": 12,
                },
            },
            {
                "claim_id": "claim-status-state",
                "subject": "艾琳",
                "predicate": "status.state",
                "object_or_value": {
                    "status_name": "沉默",
                    "stacks": 1,
                },
            },
            {
                "claim_id": "claim-binding-state",
                "subject": "艾琳",
                "predicate": "binding.state",
                "object_or_value": {
                    "source_name": "学院法杖",
                    "source_type": "item",
                    "ability_ids": ["火球术"],
                },
            },
            {
                "claim_id": "claim-qualification-state",
                "subject": "艾琳",
                "predicate": "qualification.state",
                "object_or_value": {
                    "qualification_name": "二环许可",
                    "quantity": 1,
                },
            },
            {
                "claim_id": "claim-observation",
                "subject": "艾琳",
                "predicate": "observation.capability",
                "object_or_value": {
                    "subject_entity_id": "ent-aabb",
                    "ability_name": "火球术",
                    "observed_fields": ["effects"],
                    "confidence": 0.8,
                },
                "knowledge_plane": "actor_belief",
            },
        ]
        package = build_power_package(
            {"power_profile": "magic"},
            claims,
            mode="ingest",
        )
        self.assertEqual("学院法术体系", package["power_systems"][0]["name"])
        self.assertEqual(1, len(package["progression_tracks"]))
        self.assertEqual(2, len(package["rank_nodes"]))
        self.assertEqual(1, len(package["rank_edges"]))
        self.assertEqual(2, len(package["resource_definitions"]))
        self.assertEqual(1, len(package["status_definitions"]))
        self.assertEqual(1, len(package["qualification_definitions"]))
        self.assertEqual(1, len(package["counter_rules"]))
        self.assertEqual(1, len(package["conversion_rules"]))
        actor = package["actor_power_bootstrap"][0]
        self.assertEqual(1, len(actor["progression_states"]))
        self.assertEqual(1, len(actor["resources"]))
        self.assertEqual(1, len(actor["statuses"]))
        self.assertEqual(1, len(actor["bindings"]))
        self.assertEqual(1, len(actor["qualifications"]))
        self.assertEqual(1, len(actor["observed_capabilities"]))
        self.assertTrue(
            all(binding["target_ids"] for binding in package["claim_bindings"])
        )
        self.assertEqual([], package["semantic_losses"])

    def test_normalized_v2_roundtrip_and_invalid_negotiation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = PlotInitService(temporary)
            result = service.dry_run(
                project_root=Path(temporary) / "novel",
                mode="new",
                interaction_profile="deep",
                seed=cultivation_seed(),
                bundle_schema_version="plot-rag-init/v2",
            )
            envelope = export_normalized_bundle(result["bundle"])
            restored = parse_normalized_export(envelope)
            self.assertEqual(result["bundle"]["bundle_hash"], restored["bundle_hash"])
            self.assertEqual("plot-rag-init/v2", envelope["schema_version"])
            with self.assertRaises(PlotInitError) as caught:
                service.dry_run(
                    mode="new",
                    seed=complete_seed(),
                    bundle_schema_version="plot-rag-init/v9",
                )
            self.assertEqual(
                "INVALID_INITIALIZATION_SCHEMA",
                caught.exception.code,
            )

    def test_all_declared_json_schemas_parse(self) -> None:
        paths = [
            *sorted((PLUGIN_ROOT / "schemas" / "plot-rag-power" / "v1").glob("*.json")),
            *sorted((PLUGIN_ROOT / "schemas" / "plot-rag-init" / "v2").glob("*.json")),
            *sorted((PLUGIN_ROOT / "knowledge" / "power_adapters").glob("*.json")),
        ]
        self.assertGreaterEqual(len(paths), 29)
        for path in paths:
            with self.subTest(path=path.name):
                self.assertIsInstance(
                    json.loads(path.read_text(encoding="utf-8-sig")),
                    dict,
                )


if __name__ == "__main__":
    unittest.main()
