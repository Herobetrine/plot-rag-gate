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


class OutlineScopeGateTests(unittest.TestCase):
    def test_outline_timeless_world_rule_is_forced_to_planned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ContinuityService(Path(temporary))
            host = HostApprovalAuthority(
                service,
                issuer="outline-scope-test",
                channel="interactive_test",
            )
            proposal = service.save_proposal(
                events=[
                    {
                        "event_type": "world_rule",
                        "field": "magic.cost",
                        "value": "blood",
                        "scope": "timeless",
                    }
                ],
                artifact_id="outline-1",
                artifact_stage="outline",
                branch_id="main",
                chapter_no=1,
                scene_index=0,
            )
            grant = host.issue(
                proposal["proposal_id"],
                expected_canon_revision=0,
            )
            commit = service.accept_proposal(
                proposal["proposal_id"],
                approval_id=grant["approval_id"],
                expected_canon_revision=0,
            )

            self.assertEqual("planned", commit["events"][0]["scope"])
            self.assertEqual(
                {"head": 1, "active": 1},
                service.get_canon_revisions(),
            )
            self.assertEqual(
                [],
                service.query_facts(
                    scope="timeless",
                    include_timeless=True,
                )["facts"],
            )
            self.assertEqual([], service.query_facts()["facts"])
            planned = service.query_facts(
                scope="planned",
                include_timeless=False,
            )["facts"]
            self.assertEqual(1, len(planned))
            self.assertEqual("planned", planned[0]["scope"])
            self.assertEqual(
                [],
                service.query_facts(
                    scope="planned",
                    chapter_no=0,
                    scene_index=99,
                    include_timeless=False,
                )["facts"],
            )
            self.assertEqual(
                1,
                len(
                    service.query_facts(
                        scope="planned",
                        chapter_no=1,
                        scene_index=0,
                        include_timeless=False,
                    )["facts"]
                ),
            )
            public = query_continuity(
                Path(temporary),
                scope="planned",
                chapter_no=1,
                scene_index=0,
                include_relations=False,
            )
            self.assertEqual(1, len(public["facts"]))
            self.assertEqual("planned", public["facts"][0]["scope"])
            with service.store.read_connection() as connection:
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM planned_facts"
                    ).fetchone()[0],
                )


if __name__ == "__main__":
    unittest.main()
