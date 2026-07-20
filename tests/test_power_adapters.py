from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from dataclasses import replace
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from power_system import (  # noqa: E402
    ADAPTER_CONTRACT_OPERATIONS,
    AdapterRegistry,
    AdapterSpec,
    PowerModelError,
    adapter_registry,
    build_power_package,
    canonical_power_hash,
    compile_query_terms,
    detect_native_terms,
    detect_power_profile,
    get_adapter,
    normalize_power_package,
    normalize_actor_state,
    normalize_definition,
    normalize_event,
    power_sufficiency,
    validate_transition,
    validate_power_package,
)
from power_system.adapters import (  # noqa: E402
    ADAPTER_SCHEMA_VERSION,
    render_native_projection,
    report_semantic_loss,
)
from power_system.model import POWER_PROFILES, TRACK_KINDS  # noqa: E402


PROFILE_DETECTION_TEXT = {
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

PROFILE_SEMANTIC_ANCHORS = {
    "cultivation": {
        "dimensions": {"source", "breakthrough", "failure"},
        "native_categories": {"progression_track", "ability", "resource_pool"},
    },
    "magic": {
        "dimensions": {"learned", "prepared", "resource_cycle", "interrupt"},
        "native_categories": {"progression_track", "ability", "qualification"},
    },
    "skill_tree": {
        "dimensions": {"prerequisite", "mutual_exclusion", "point_cost"},
        "native_categories": {"progression_track", "ability", "qualification"},
    },
    "game": {
        "dimensions": {"level_axes", "quest_evidence", "equipment_slot"},
        "native_categories": {"progression_track", "resource_pool", "qualification"},
    },
    "martial": {
        "dimensions": {"stance", "distance", "injury", "meridian"},
        "native_categories": {"ability", "resource_pool", "status_effect"},
    },
    "superpower": {
        "dimensions": {"awakening", "load", "control", "loss_of_control"},
        "native_categories": {"ability", "resource_pool", "status_effect"},
    },
    "bloodline": {
        "dimensions": {"inheritance", "purity", "compatibility", "rejection"},
        "native_categories": {"progression_track", "binding", "status_effect"},
    },
    "technology": {
        "dimensions": {"energy", "heat", "authorization", "maintenance"},
        "native_categories": {"ability", "resource_pool", "qualification"},
    },
    "contract_summoning": {
        "dimensions": {"slot", "control_range", "shared_resource", "backlash"},
        "native_categories": {"ability", "binding", "status_effect"},
    },
    "system_assist": {
        "dimensions": {"panel_plane", "task_condition", "reward_source", "permission"},
        "native_categories": {"ability", "resource_pool", "observation"},
    },
    "hybrid": {
        "dimensions": {"namespaces", "isolation", "bridge", "conversion"},
        "native_categories": {"power_system", "bridge_rule", "counter_rule"},
    },
    "mundane": {
        "dimensions": {"explicit_not_applicable"},
        "native_categories": {"progression_track", "resource_pool", "qualification"},
    },
}


def _first_term(adapter: AdapterSpec, category: str, fallback: str) -> str:
    return str((adapter.native_terms.get(category) or (fallback,))[0])


def _complete_profile_dossier(profile: str) -> dict[str, object]:
    adapter = get_adapter(profile)
    namespace = f"adapter-test.{profile}"
    track_namespace = f"{namespace}.track"
    track_name = _first_term(adapter, "progression_track", "成长轨")
    ability_name = _first_term(adapter, "ability", "代表能力")
    resource_name = _first_term(adapter, "resource_pool", "能量")
    dossier: dict[str, object] = {
        "power_profile": profile,
        "power_systems": [
            {
                "namespace": namespace,
                "name": adapter.display_name,
                "profile": profile,
                "social_consequences": ["身份与权限发生变化"],
                "no_resource_pool": profile == "mundane",
            }
        ],
    }
    if profile == "mundane":
        return dossier
    track_kind = next(kind for kind in adapter.track_kinds if kind != "none")
    dossier.update(
        {
            "progression_tracks": [
                {
                    "namespace": track_namespace,
                    "name": track_name,
                    "track_kind": track_kind,
                }
            ],
            "rank_nodes": [
                {
                    "track_namespace": track_namespace,
                    "name": "起点",
                    "order": 1,
                },
                {
                    "track_namespace": track_namespace,
                    "name": "进阶",
                    "order": 2,
                    "social_consequences": ["获得新的制度权限"],
                },
            ],
            "rank_edges": [
                {
                    "track_namespace": track_namespace,
                    "from_node_ids": ["起点"],
                    "to_node_id": "进阶",
                    "prerequisites": {"qualification": "已满足"},
                    "resource_costs": [{"resource": resource_name, "amount": 1}],
                    "failure_outcomes": ["受伤并失去资源"],
                }
            ],
            "ability_definitions": [
                {
                    "name": ability_name,
                    "source_bindings": ["可追溯传承"],
                    "costs": [resource_name],
                    "limits": ["使用后进入冷却"],
                    "counters": ["存在明确反制窗口"],
                }
            ],
            "resource_definitions": [
                {
                    "name": resource_name,
                    "acquisition": ["训练或补给"],
                    "consumption": ["使用能力"],
                    "recovery": ["休息或维护"],
                }
            ],
            "actor_power_bootstrap": [
                {
                    "actor_name": "甲",
                    "progression_states": [
                        {
                            "track_namespace": track_namespace,
                            "current_rank": "起点",
                        }
                    ],
                    "ability_ownerships": [{"ability_name": ability_name}],
                    "resources": [
                        {
                            "resource_name": resource_name,
                            "balance": 10,
                        }
                    ],
                }
            ],
        }
    )
    return dossier


class PowerAdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = adapter_registry()

    def test_profile_files_form_complete_versioned_contract(self) -> None:
        adapter_root = PLUGIN_ROOT / "knowledge" / "power_adapters"
        paths = sorted(adapter_root.glob("*.json"))
        self.assertEqual(set(POWER_PROFILES), {path.stem for path in paths})
        seen_ids: set[str] = set()
        for path in paths:
            with self.subTest(profile=path.stem):
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                adapter = AdapterSpec.from_mapping(payload)
                self.assertEqual(ADAPTER_SCHEMA_VERSION, payload["schema_version"])
                self.assertEqual(path.stem, adapter.profile)
                self.assertEqual(
                    f"plot-rag-power.{adapter.profile}",
                    adapter.adapter_id,
                )
                self.assertRegex(adapter.version, r"^\d+\.\d+\.\d+$")
                self.assertNotIn(adapter.adapter_id, seen_ids)
                seen_ids.add(adapter.adapter_id)
                self.assertTrue(adapter.display_name.strip())
                self.assertTrue(adapter.aliases)
                self.assertTrue(adapter.detection_terms)
                self.assertTrue(adapter.track_kinds)
                self.assertTrue(set(adapter.track_kinds).issubset(TRACK_KINDS))
                self.assertTrue(adapter.native_terms)
                self.assertTrue(
                    all(
                        key.strip() and terms and all(term.strip() for term in terms)
                        for key, terms in adapter.native_terms.items()
                    )
                )
                self.assertTrue(adapter.required_dimensions)
                self.assertTrue(adapter.question_prompts)
                self.assertEqual(
                    adapter.profile == "mundane",
                    adapter.no_rank_generation,
                )

    def test_adapter_roundtrip_and_registry_fail_closed(self) -> None:
        adapters = list(self.registry.all())
        for adapter in adapters:
            with self.subTest(profile=adapter.profile):
                self.assertEqual(
                    adapter,
                    AdapterSpec.from_mapping(adapter.to_dict()),
                )
                self.assertEqual(adapter, self.registry.get(adapter.adapter_id))
                self.assertEqual(adapter, self.registry.get(adapter.profile))

        with self.assertRaises(PowerModelError) as caught:
            AdapterSpec.from_mapping(
                {
                    "schema_version": "plot-rag-power-adapter/v9",
                    "profile": "magic",
                }
            )
        self.assertEqual("POWER_ADAPTER_SCHEMA_UNSUPPORTED", caught.exception.code)

        with self.assertRaises(PowerModelError) as caught:
            AdapterSpec.from_mapping(
                {
                    "schema_version": ADAPTER_SCHEMA_VERSION,
                    "profile": "unknown-profile",
                }
            )
        self.assertEqual("POWER_PROFILE_UNSUPPORTED", caught.exception.code)

        with self.assertRaises(PowerModelError) as caught:
            AdapterRegistry(adapters[:-1])
        self.assertEqual("POWER_ADAPTER_SET_INCOMPLETE", caught.exception.code)

        with self.assertRaises(PowerModelError) as caught:
            AdapterRegistry([*adapters, adapters[0]])
        self.assertEqual("POWER_ADAPTER_DUPLICATE_PROFILE", caught.exception.code)

        duplicate_id = replace(adapters[1], adapter_id=adapters[0].adapter_id)
        with self.assertRaises(PowerModelError) as caught:
            AdapterRegistry([adapters[0], duplicate_id, *adapters[2:]])
        self.assertEqual("POWER_ADAPTER_DUPLICATE_ID", caught.exception.code)

        with self.assertRaises(PowerModelError) as caught:
            self.registry.get("not-registered")
        self.assertEqual("POWER_ADAPTER_NOT_FOUND", caught.exception.code)

    def test_profile_detection_explicit_override_and_hybrid_tie(self) -> None:
        for profile, text in PROFILE_DETECTION_TEXT.items():
            with self.subTest(profile=profile):
                self.assertEqual(profile, detect_power_profile(text))
        self.assertEqual(
            "magic",
            detect_power_profile(
                PROFILE_DETECTION_TEXT["cultivation"],
                explicit_profile="magic",
            ),
        )
        self.assertEqual("mundane", detect_power_profile("普通的未分类文本"))
        self.assertEqual("hybrid", detect_power_profile("血统武侠"))

    def test_query_terms_native_projection_and_semantic_loss(self) -> None:
        for adapter in self.registry.all():
            category = next(iter(adapter.native_terms))
            native_term = adapter.native_terms[category][0]
            project_term = f"{adapter.profile}-project-term"
            with self.subTest(profile=adapter.profile):
                terms = compile_query_terms(
                    adapter.profile,
                    categories=(category,),
                    project_terms={category: (project_term,)},
                )
                for expected in (
                    adapter.display_name,
                    adapter.profile,
                    *adapter.aliases,
                    native_term,
                    project_term,
                ):
                    self.assertIn(expected, terms)
                self.assertEqual(
                    native_term,
                    render_native_projection(
                        adapter.profile,
                        category,
                        "fallback",
                    ),
                )
                self.assertEqual(
                    "fallback",
                    render_native_projection(
                        adapter.profile,
                        "unmapped-category",
                        "fallback",
                    ),
                )
                lossless = report_semantic_loss(
                    adapter.profile,
                    category,
                    native_term,
                )
                self.assertEqual("lossless", lossless["mapping_quality"])
                self.assertEqual([], lossless["semantic_loss"])
                partial = report_semantic_loss(
                    adapter.profile,
                    category,
                    project_term,
                )
                self.assertEqual("partial", partial["mapping_quality"])
                self.assertTrue(partial["semantic_loss"])

    def test_all_profiles_build_deterministic_minimal_power_specs(self) -> None:
        for profile in sorted(POWER_PROFILES):
            with self.subTest(profile=profile):
                dossier = _complete_profile_dossier(profile)
                first = build_power_package(dossier, mode="new")
                second = build_power_package(copy.deepcopy(dossier), mode="new")
                validate_power_package(first)
                self.assertEqual(first, second)
                self.assertEqual(
                    canonical_power_hash(first),
                    first["power_package_hash"],
                )
                normalized_again = normalize_power_package(first)
                self.assertEqual(
                    first["power_package_hash"],
                    normalized_again["power_package_hash"],
                )
                system = first["power_systems"][0]
                adapter = get_adapter(profile)
                self.assertEqual(adapter.adapter_id, system["adapter_id"])
                self.assertEqual(adapter.version, system["adapter_version"])
                self.assertTrue(system["native_term_bindings"])
                self.assertTrue(
                    all(
                        binding["adapter_id"] == adapter.adapter_id
                        and binding["adapter_version"] == adapter.version
                        for binding in system["native_term_bindings"]
                    )
                )
                self.assertTrue(power_sufficiency(first, mode="new")["sufficient"])
                if profile == "mundane":
                    self.assertEqual([], first["progression_tracks"])
                    self.assertEqual([], first["rank_nodes"])
                    self.assertEqual([], first["rank_edges"])
                    self.assertEqual("not_applicable", first["power_model_status"])
                else:
                    self.assertEqual(1, len(first["progression_tracks"]))
                    self.assertEqual(2, len(first["rank_nodes"]))
                    self.assertEqual(1, len(first["rank_edges"]))
                    actor = first["actor_power_bootstrap"][0]
                    self.assertIn(
                        actor["ability_ownerships"][0]["ability_id"],
                        {
                            item["ability_id"]
                            for item in first["ability_definitions"]
                        },
                    )
                    self.assertIn(
                        actor["resources"][0]["resource_id"],
                        {
                            item["resource_id"]
                            for item in first["resource_definitions"]
                        },
                    )

    def test_no_profile_invents_rank_chain_from_genre_terms(self) -> None:
        for profile in sorted(POWER_PROFILES):
            with self.subTest(profile=profile):
                package = build_power_package(
                    {
                        "power_profile": profile,
                        "genre_contract": PROFILE_DETECTION_TEXT[profile],
                    },
                    mode="new",
                )
                self.assertEqual([], package["progression_tracks"])
                self.assertEqual([], package["rank_nodes"])
                self.assertEqual([], package["rank_edges"])
                if profile == "mundane":
                    self.assertTrue(
                        power_sufficiency(package, mode="new")["sufficient"]
                    )
                else:
                    self.assertFalse(
                        power_sufficiency(package, mode="new")["sufficient"]
                    )

    def test_uniform_invalid_semantics_fail_closed_for_every_profile(self) -> None:
        for profile in sorted(POWER_PROFILES - {"mundane"}):
            package = build_power_package(
                _complete_profile_dossier(profile),
                mode="new",
            )
            with self.subTest(profile=profile, fault="track-kind"):
                invalid = copy.deepcopy(package)
                invalid["progression_tracks"][0]["track_kind"] = "power-score"
                with self.assertRaises(PowerModelError) as caught:
                    validate_power_package(invalid)
                self.assertEqual(
                    "POWER_TRACK_KIND_UNSUPPORTED",
                    caught.exception.code,
                )

            with self.subTest(profile=profile, fault="cross-track-edge"):
                invalid = copy.deepcopy(package)
                second_track = copy.deepcopy(invalid["progression_tracks"][0])
                second_track["track_id"] = f"{second_track['track_id']}-other"
                invalid["progression_tracks"].append(second_track)
                second_node = copy.deepcopy(invalid["rank_nodes"][1])
                second_node["rank_node_id"] = f"{second_node['rank_node_id']}-other"
                second_node["track_id"] = second_track["track_id"]
                invalid["rank_nodes"].append(second_node)
                invalid["rank_edges"][0]["to_node_id"] = second_node["rank_node_id"]
                with self.assertRaises(PowerModelError) as caught:
                    validate_power_package(invalid)
                self.assertEqual("POWER_TRACK_MISMATCH", caught.exception.code)

            for field, missing_id in (
                ("ability_ownerships", "ent-missing-ability"),
                ("resources", "ent-missing-resource"),
            ):
                with self.subTest(profile=profile, fault=field):
                    invalid = copy.deepcopy(package)
                    if field == "ability_ownerships":
                        invalid["actor_power_bootstrap"][0][field][0][
                            "ability_id"
                        ] = missing_id
                    else:
                        invalid["actor_power_bootstrap"][0][field][0][
                            "resource_id"
                        ] = missing_id
                    with self.assertRaises(PowerModelError) as caught:
                        validate_power_package(invalid)
                    self.assertEqual(
                        "POWER_ENDPOINT_UNRESOLVED",
                        caught.exception.code,
                    )

    def test_profile_semantic_anchors_are_not_empty_shells(self) -> None:
        self.assertEqual(set(POWER_PROFILES), set(PROFILE_SEMANTIC_ANCHORS))
        for profile, expected in PROFILE_SEMANTIC_ANCHORS.items():
            adapter = get_adapter(profile)
            with self.subTest(profile=profile):
                self.assertTrue(
                    expected["dimensions"].issubset(
                        set(adapter.required_dimensions)
                    )
                )
                self.assertTrue(
                    expected["native_categories"].issubset(
                        set(adapter.native_terms)
                    )
                )
                combined_prompts = " ".join(adapter.question_prompts)
                self.assertGreaterEqual(len(combined_prompts.strip()), 12)
                self.assertFalse(
                    re.search(r"\b(?:todo|tbd|unknown)\b", combined_prompts, re.I)
                )

    def test_every_profile_exposes_and_executes_the_full_adapter_contract(
        self,
    ) -> None:
        actor_id = "ent-a100"
        track_id = "ent-b100"
        rank_one = "ent-c100"
        rank_two = "ent-c200"
        rank_illegal = "ent-c300"
        ability_id = "ent-d100"
        resource_id = "ent-e100"
        status_id = "ent-f100"
        qualification_id = "ent-a200"
        binding_id = "ent-b200"
        for profile in sorted(POWER_PROFILES):
            adapter = get_adapter(profile)
            with self.subTest(profile=profile, contract="surface"):
                self.assertEqual(
                    ADAPTER_CONTRACT_OPERATIONS,
                    adapter.contract_operations,
                )
                detected = detect_native_terms(
                    profile,
                    " ".join(
                        term
                        for terms in adapter.native_terms.values()
                        for term in terms[:1]
                    ),
                )
                self.assertEqual(adapter.adapter_id, detected["adapter_id"])
                self.assertTrue(detected["matches"])
                native_ability = _first_term(
                    adapter,
                    "ability",
                    f"{profile}-ability",
                )
                definition = normalize_definition(
                    profile,
                    "ability",
                    {"name": native_ability},
                )
                self.assertEqual(native_ability, definition["name"])
                self.assertEqual(adapter.adapter_id, definition["adapter_id"])
                self.assertTrue(definition["native_term_bindings"])

                actor = normalize_actor_state(
                    profile,
                    {
                        "actor_name": "甲",
                        "progression_states": [
                            {
                                "track_id": track_id,
                                "rank_node_id": rank_one,
                            }
                        ],
                        "ability_ownerships": [
                            {
                                "ability_id": ability_id,
                                "unlock_state": "unlocked",
                            }
                        ],
                        "resources": [
                            {
                                "resource_id": resource_id,
                                "amount": 10,
                            }
                        ],
                        "bindings": [{"binding_id": binding_id}],
                        "qualifications": [
                            {
                                "qualification_id": qualification_id,
                                "quantity": 1,
                            }
                        ],
                    },
                )
                self.assertRegex(actor["actor_id"], r"^ent-[a-f0-9]+$")
                normalized_event = normalize_event(
                    profile,
                    {
                        "category": "升级",
                        "action": "突破",
                        "from_rank_entity_id": rank_one,
                        "to_rank_entity_id": rank_two,
                    },
                )
                self.assertEqual("progression", normalized_event["event_type"])
                self.assertEqual("advance", normalized_event["action"])

            actor_state = {
                "actor_id": actor_id,
                "rank_node_id": rank_one,
                "ability_ownerships": [
                    {
                        "ability_id": ability_id,
                        "unlock_state": "unlocked",
                        "available": True,
                        "source_active": True,
                    }
                ],
                "resources": [
                    {
                        "resource_id": resource_id,
                        "amount": 10,
                    }
                ],
                "bindings": [{"binding_id": binding_id}],
            }
            power_spec = {
                "power_model_status": "modeled",
                "rank_edges": [
                    {
                        "from_node_ids": [rank_one],
                        "to_node_id": rank_two,
                    }
                ],
                "status_definitions": [{"status_id": status_id}],
                "qualification_definitions": [
                    {"qualification_id": qualification_id}
                ],
                "conversion_rules": [],
            }
            with self.subTest(profile=profile, contract="transition"):
                allowed = validate_transition(
                    profile,
                    {
                        "event_type": "progression",
                        "action": "advance",
                        "from_rank_entity_id": rank_one,
                        "to_rank_entity_id": rank_two,
                    },
                    actor_state=actor_state,
                    power_spec=power_spec,
                )
                self.assertEqual("allowed", allowed["status"])
                blocked = adapter.validate_transition(
                    {
                        "event_type": "progression",
                        "action": "advance",
                        "from_rank_entity_id": rank_one,
                        "to_rank_entity_id": rank_illegal,
                    },
                    actor_state=actor_state,
                    power_spec=power_spec,
                )
                self.assertEqual("blocked", blocked["status"])
                self.assertIn(
                    "POWER_TRANSITION_EDGE_MISSING",
                    blocked["reason_codes"],
                )

                ability_allowed = adapter.validate_transition(
                    {
                        "event_type": "ability",
                        "action": "use",
                        "ability_entity_id": ability_id,
                    },
                    actor_state=actor_state,
                    power_spec=power_spec,
                )
                self.assertEqual("allowed", ability_allowed["status"])
                cooldown_state = copy.deepcopy(actor_state)
                cooldown_state["ability_ownerships"][0][
                    "cooldown_active"
                ] = True
                cooldown = adapter.validate_transition(
                    {
                        "event_type": "ability",
                        "action": "use",
                        "ability_entity_id": ability_id,
                    },
                    actor_state=cooldown_state,
                    power_spec=power_spec,
                )
                self.assertEqual("blocked", cooldown["status"])
                self.assertIn("POWER_COOLDOWN_ACTIVE", cooldown["reason_codes"])

                inactive_source = copy.deepcopy(actor_state)
                inactive_source["ability_ownerships"][0][
                    "source_active"
                ] = False
                source_result = adapter.validate_transition(
                    {
                        "event_type": "ability",
                        "action": "use",
                        "ability_entity_id": ability_id,
                    },
                    actor_state=inactive_source,
                    power_spec=power_spec,
                )
                self.assertEqual("blocked", source_result["status"])
                self.assertIn(
                    "POWER_SOURCE_INACTIVE",
                    source_result["reason_codes"],
                )

                binding = adapter.validate_transition(
                    {
                        "event_type": "power_binding",
                        "action": "unbind",
                        "binding_id": binding_id,
                    },
                    actor_state=actor_state,
                    power_spec=power_spec,
                )
                self.assertEqual("allowed", binding["status"])
                unknown = adapter.validate_transition(
                    {
                        "event_type": "ability",
                        "action": "use",
                        "ability_entity_id": ability_id,
                        "field_status": "deferred",
                    },
                    actor_state=actor_state,
                    power_spec=power_spec,
                )
                self.assertEqual("unknown", unknown["status"])
                self.assertIn(
                    "POWER_MODEL_STATE_UNKNOWN",
                    unknown["reason_codes"],
                )


if __name__ == "__main__":
    unittest.main()
