from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from continuity import ContinuityService  # noqa: E402
import v1_runtime as v1  # noqa: E402


def _file_state(root: Path) -> dict[str, tuple[int, int, str]]:
    state: dict[str, tuple[int, int, str]] = {}
    for path in sorted(
        (candidate for candidate in root.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.as_posix(),
    ):
        stat = path.stat()
        state[path.relative_to(root).as_posix()] = (
            stat.st_size,
            stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return state


class PowerQueryReadOnlyTests(unittest.TestCase):
    def _run_all(
        self,
        root: Path,
        *,
        actor_id: str | None = None,
        other_actor_id: str | None = None,
        ability_id: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        return {
            "systems": v1.list_power_systems(root),
            "state": v1.query_power_state(
                root,
                entity_id=actor_id,
                ability_id=ability_id,
            ),
            "path": v1.query_progression_path(
                root,
                entity_id=actor_id,
            ),
            "explain": v1.explain_power_action(
                root,
                action_id="use",
                entity_id=actor_id,
                ability_id=ability_id,
            ),
            "compare": v1.compare_power_conditions(
                root,
                left_entity_id=actor_id,
                right_entity_id=other_actor_id,
                conditions={"terrain": "open"},
            ),
        }

    def test_missing_database_keeps_file_set_hash_size_and_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = _file_state(root)
            results = self._run_all(root)
            after = _file_state(root)
            self.assertEqual(before, after)
            self.assertTrue(
                all(result.get("status") == "uninitialized" for result in results.values())
            )

    def test_wal_and_shm_without_main_database_are_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / ".plot-rag"
            state_dir.mkdir(parents=True)
            (state_dir / "state.sqlite3-wal").write_bytes(b"orphan-wal")
            (state_dir / "state.sqlite3-shm").write_bytes(b"orphan-shm")
            before = _file_state(root)
            results = self._run_all(root)
            after = _file_state(root)
            self.assertEqual(before, after)
            self.assertTrue(
                all(result.get("status") == "uninitialized" for result in results.values())
            )

    def test_existing_database_is_queried_through_disposable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ContinuityService(root)
            service.schema_status()
            actor = service.register_entity("character", "甲")["entity_id"]
            other_actor = service.register_entity("character", "乙")["entity_id"]
            ability = service.register_entity("ability", "火球术")["entity_id"]
            before = _file_state(root)
            results = self._run_all(
                root,
                actor_id=str(actor),
                other_actor_id=str(other_actor),
                ability_id=str(ability),
            )
            after = _file_state(root)
            self.assertEqual(before, after)
            self.assertEqual(
                {"systems", "state", "path", "explain", "compare"},
                set(results),
            )
            self.assertTrue(all(isinstance(result, dict) for result in results.values()))


if __name__ == "__main__":
    unittest.main()
