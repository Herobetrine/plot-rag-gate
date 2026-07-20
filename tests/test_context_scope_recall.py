from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from continuity import ContinuityService, HostApprovalAuthority  # noqa: E402
from v1_runtime import build_longform_context  # noqa: E402


class ContextScopeRecallTests(unittest.TestCase):
    @staticmethod
    def _accept(
        service: ContinuityService,
        host: HostApprovalAuthority,
        *,
        event: dict[str, object],
        artifact_id: str,
        stage: str,
        chapter_no: int,
    ) -> None:
        revision = service.get_canon_revisions()["active"]
        proposal = service.save_proposal(
            events=[event],
            artifact_id=artifact_id,
            artifact_stage=stage,
            branch_id="main",
            chapter_no=chapter_no,
            scene_index=0,
            prepared_canon_revision=revision,
        )
        grant = host.issue(
            proposal["proposal_id"],
            expected_canon_revision=revision,
        )
        service.accept_proposal(
            proposal["proposal_id"],
            approval_id=grant["approval_id"],
            expected_canon_revision=revision,
        )

    def test_context_labels_planned_and_historical_without_mixing_current(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".plot-rag").mkdir()
            (root / "正文").mkdir()
            (root / "正文" / "第一章.md").write_text(
                "测试角色甲在测试城追查失踪案。",
                encoding="utf-8",
            )
            (root / ".plot-rag" / "config.json").write_text(
                json.dumps(
                    {
                        "config_version": 3,
                        "enabled": True,
                        "authority_sources": [
                            {
                                "glob": "正文/**/*.md",
                                "role": "canon",
                                "scope_policy": "infer_and_review",
                                "ingest_policy": "include",
                                "priority": 100,
                            }
                        ],
                        "remote": {
                            "embedding": {"enabled": False},
                            "rerank": {"enabled": False},
                            "extract": {"enabled": False},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            service = ContinuityService(root)
            host = HostApprovalAuthority(
                service,
                issuer="context-scope-test",
                channel="interactive_test",
            )
            actor = service.register_entity("character", "测试角色甲")["entity_id"]
            self._accept(
                service,
                host,
                event={
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "condition",
                    "value": "曾经负伤",
                    "scope": "historical",
                },
                artifact_id="history-1",
                stage="final",
                chapter_no=1,
            )
            self._accept(
                service,
                host,
                event={
                    "event_type": "state",
                    "entity_id": actor,
                    "field": "future_goal",
                    "value": "第二章潜入钟楼",
                    "scope": "current",
                },
                artifact_id="outline-2",
                stage="outline",
                chapter_no=2,
            )

            result = build_longform_context(
                root,
                "设计第二章章纲，承接测试角色甲的旧伤与潜入计划",
                artifact_context={
                    "artifact_stage": "outline",
                    "task": "outline",
                    "branch_id": "main",
                    "chapter_no": 2,
                    "scene_index": 0,
                },
                max_context_chars=4000,
            )
            context = result["context"]
            self.assertIn("[ACCEPTED_PLANNED_FACTS]", context)
            self.assertIn("第二章潜入钟楼", context)
            self.assertIn("[ACCEPTED_HISTORICAL_FACTS]", context)
            self.assertIn("曾经负伤", context)
            self.assertEqual("planned", result["planned"]["facts"][0]["scope"])
            self.assertEqual(
                "historical",
                result["historical"]["facts"][0]["scope"],
            )
            self.assertEqual([], result["precise"]["facts"])


if __name__ == "__main__":
    unittest.main()
