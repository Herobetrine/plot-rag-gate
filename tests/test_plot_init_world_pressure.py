from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from plot_init import PlotInitService  # noqa: E402
from plot_init.constants import PRESSURE_TESTS  # noqa: E402
from plot_init.engine import _pressure_tests  # noqa: E402


def full_world() -> dict[str, Any]:
    return {
        "rules": ["跨层移动必须依赖列车并支付资格与暴露成本"],
        "scarce_resources": ["通行资格"],
        "power_distribution": "维护列车者控制资源、交通与执法",
        "current_pressures": ["城际线将在七天后停运"],
        "mvw": {
            "story_clock": "景历十二年三月初七",
            "locations_and_routes": ["测试城", "测试枢纽站", "旧港"],
            "base_rules": ["跨层移动必须依赖列车"],
            "core_capability": {
                "capability": "短暂改写通行条件",
                "cost": "消耗通行证并留下记录",
                "boundary": "不能跨越停运线路",
                "counter": "列车署可冻结资格",
            },
            "survival_resource_chain": "农场→列车→市场→家庭",
            "power_scarcity_chain": "通行资格→列车署→城市阶层",
            "daily_cycles": ["普通工人的一天", "列车署官员的一天"],
            "infrastructure_bottleneck": "单线列车容量有限",
            "formal_institution_chain": "发现→报告→裁决→执行",
            "power_actors": ["列车署", "旧港商会"],
            "harmed_group": "依赖跨层通勤的工人",
            "legitimacy_narrative": "停运是为了保护城市安全",
            "important_secret": "停运源于通行资格系统失控",
            "historical_trauma": "十年前列车事故仍塑造禁忌",
            "pressure_horizons": {
                "near": "七天停运",
                "volume": "城市断供",
                "book": "跨层秩序失去合法性",
            },
            "irreversible_trigger": "主角取得一张被注销的通行证",
        },
    }


def rich_seed() -> str:
    return "\n".join(
        [
            "题材：都市异能悬疑",
            "读者承诺：每个案件都改变主角的资源与关系",
            "核心规则：能力使用会留下可追踪的记忆裂痕",
            "稀缺资源：未被污染的旧时代档案",
            "主角：测试角色甲",
            "对手：镜像客",
            "外在目标：在七日内找回失踪档案",
            "失败代价：妹妹的身份会被系统抹除",
            "触发事件：封存档案在众目睽睽下自行改写",
            "第一卷变化：测试角色甲从调查员变成制度通缉对象",
            "故事时间：景历十二年三月初七",
            "地点：测试城与测试枢纽站",
            "连载：章级线索兑现、卷级身份翻转",
            "当前压力：列车署正在清洗所有相关见证者",
            "权力分配：列车署控制跨区交通与身份登记",
            "核心能力：读取物品残留的短时记忆",
            "差异化：制度悬疑与城市生存并行",
            "终局问题：谁有权决定一段记忆是否真实",
            "补充：" + "世界会在主角不行动时继续运转。" * 30,
        ]
    )


class PlotInitWorldPressureTestCase(unittest.TestCase):
    def test_fixed_ids_and_full_world_pass_with_observed_evidence(self) -> None:
        results = _pressure_tests(full_world(), [])
        expected_ids = [test_id for test_id, _ in PRESSURE_TESTS]

        self.assertEqual(expected_ids, [item["test_id"] for item in results])
        self.assertEqual(10, len(results))
        self.assertTrue(all(item["status"] == "pass" for item in results))
        for item in results:
            self.assertEqual(
                len(item["required_evidence"]),
                len(item["observed_evidence"]),
            )
            self.assertTrue(
                all(
                    observation["satisfied"]
                    for observation in item["observed_evidence"]
                )
            )
            self.assertEqual(
                len(item["required_evidence"]),
                item["score"]["satisfied"],
            )
            self.assertEqual(
                "all_required_evidence_satisfied",
                item["reason"],
            )

    def test_missing_world_evidence_is_explained_as_degraded_or_fail(self) -> None:
        cases = {
            "rules": (
                "optimal_exploitation",
                ("world_rules", "base_rules"),
                lambda world: (
                    world.update({"rules": []}),
                    world["mvw"].update({"base_rules": []}),
                ),
            ),
            "resources": (
                "supply_cut",
                (
                    "scarce_resources",
                    "survival_resource_chain",
                    "infrastructure_bottleneck",
                ),
                lambda world: (
                    world.update({"scarce_resources": []}),
                    world["mvw"].update(
                        {
                            "survival_resource_chain": None,
                            "infrastructure_bottleneck": None,
                        }
                    ),
                ),
            ),
            "power": (
                "power_vacuum",
                (
                    "power_distribution",
                    "power_actors",
                    "formal_institution_chain",
                    "power_scarcity_chain",
                ),
                lambda world: (
                    world.update({"power_distribution": None}),
                    world["mvw"].update(
                        {
                            "power_actors": [],
                            "formal_institution_chain": None,
                            "power_scarcity_chain": None,
                        }
                    ),
                ),
            ),
            "daily": (
                "ordinary_day",
                ("daily_cycles",),
                lambda world: world["mvw"].update({"daily_cycles": []}),
            ),
            "pressure": (
                "plot_fertility",
                (
                    "current_pressures",
                    "pressure_horizons",
                    "irreversible_trigger",
                ),
                lambda world: (
                    world.update({"current_pressures": []}),
                    world["mvw"].update(
                        {
                            "pressure_horizons": None,
                            "irreversible_trigger": None,
                        }
                    ),
                ),
            ),
            "history": (
                "historical_counterfactual",
                ("historical_trauma",),
                lambda world: world["mvw"].update(
                    {"historical_trauma": None}
                ),
            ),
        }

        for category, (test_id, missing_ids, mutate) in cases.items():
            with self.subTest(category=category):
                world = full_world()
                mutate(world)
                result = {
                    item["test_id"]: item
                    for item in _pressure_tests(world, [])
                }[test_id]
                self.assertIn(result["status"], {"degraded", "fail"})
                self.assertTrue(result["reason"].startswith("missing_evidence:"))
                self.assertIn(
                    "missing or structurally insufficient",
                    result["diagnostic"],
                )
                observed = {
                    item["evidence_id"]: item
                    for item in result["observed_evidence"]
                }
                for evidence_id in missing_ids:
                    self.assertFalse(
                        observed[evidence_id]["satisfied"],
                        evidence_id,
                    )
                    self.assertIn(evidence_id, result["reason"])

    def test_output_is_deterministic_and_sensitive_to_structure(self) -> None:
        claims = [
            {
                "claim_id": "claim-rule",
                "predicate": "world.rule",
            },
            {
                "claim_id": "claim-pressure",
                "predicate": "world.pressure",
            },
        ]
        first = _pressure_tests(copy.deepcopy(full_world()), claims)
        second = _pressure_tests(copy.deepcopy(full_world()), claims)
        self.assertEqual(first, second)

        malformed = full_world()
        malformed["mvw"]["daily_cycles"] = ["同一个人的一天", "同一个人的一天"]
        malformed["mvw"]["core_capability"].pop("counter")
        changed = _pressure_tests(malformed, claims)
        changed_by_id = {item["test_id"]: item for item in changed}
        self.assertEqual("degraded", changed_by_id["ordinary_day"]["status"])
        self.assertEqual(
            "degraded",
            changed_by_id["optimal_exploitation"]["status"],
        )
        self.assertIn(
            "claim-rule",
            changed_by_id["optimal_exploitation"]["source_claim_ids"],
        )

    def test_rich_seed_bundle_remains_schema_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            result = PlotInitService(workspace).dry_run(
                project_root=workspace / "novel",
                mode="new",
                target_profile="plot_ready",
                interaction_profile="deep",
                seed=rich_seed(),
            )

        bundle = result["bundle"]
        pressure_tests = bundle["world_model"]["pressure_tests"]
        self.assertEqual("plot-rag-init/v1", bundle["schema_version"])
        self.assertEqual(10, len(pressure_tests))
        self.assertTrue(all(item["status"] == "pass" for item in pressure_tests))
        self.assertEqual(
            {
                item["test_id"]: item["status"]
                for item in pressure_tests
            },
            bundle["validation"]["pressure_tests"],
        )
        for item in pressure_tests:
            self.assertIn("evidence_fields", item)
            self.assertIn("source_claim_ids", item)
            self.assertIn("notes", item)
            self.assertIn("required_evidence", item)
            self.assertIn("observed_evidence", item)
            self.assertIn("diagnostic", item)
            self.assertIn("reason", item)
        json.dumps(bundle, ensure_ascii=False, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
