from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from continuity import ContinuityService, HostApprovalAuthority  # noqa: E402
from v1_runtime import query_continuity  # noqa: E402


class QuerySurfaceTests(unittest.TestCase):
    def test_relations_are_exposed_on_service_and_public_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ContinuityService(root)
            actor = service.register_entity("character", "测试角色甲")["entity_id"]
            ally = service.register_entity("character", "林岚")["entity_id"]
            proposal = service.save_proposal(
                events=[
                    {
                        "event_type": "relation",
                        "source_entity_id": actor,
                        "target_entity_id": ally,
                        "dimension": "trust",
                        "value": 0.75,
                    }
                ],
                artifact_id="chapter-1",
                artifact_stage="final",
                branch_id="main",
                chapter_no=1,
                scene_index=0,
            )
            host = HostApprovalAuthority(
                service,
                issuer="query-surface-test",
                channel="interactive_test",
            )
            grant = host.issue(
                proposal["proposal_id"],
                expected_canon_revision=0,
            )
            service.accept_proposal(
                proposal["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )

            direct = service.query_relations(actor)
            self.assertEqual(direct["facts"], direct["relations"])
            self.assertEqual("trust", direct["relations"][0]["field"])

            public = query_continuity(root, mention="测试角色甲")
            self.assertEqual(1, len(public["relations"]))
            self.assertEqual("trust", public["relations"][0]["field"])


if __name__ == "__main__":
    unittest.main()
