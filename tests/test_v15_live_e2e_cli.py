from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import benchmarks.run_v15_live_e2e as live_cli
from benchmarks.v15_live_e2e import compare_tree_snapshots, tree_snapshot


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "benchmarks"
    / "fixtures"
    / "v15_generic_live_prompts.v1.json"
)


class V15LiveE2ECLITests(unittest.TestCase):
    def make_project(self, root: Path) -> Path:
        project = root / "novel"
        (project / ".plot-rag").mkdir(parents=True)
        (project / "正文").mkdir()
        (project / ".plot-rag" / "config.json").write_text(
            (ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        (project / "正文" / "第一章.md").write_text(
            "角色甲位于基准南站。\n",
            encoding="utf-8",
        )
        return project

    def test_validate_command_is_read_only_and_does_not_dispatch_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            before = tree_snapshot(project)
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    live_cli,
                    "run_v15_live_e2e",
                    side_effect=AssertionError("run dispatched"),
                ),
                mock.patch.object(
                    live_cli,
                    "write_redacted_report",
                    side_effect=AssertionError("report written"),
                ),
                mock.patch(
                    "benchmarks.v15_live_e2e.shutil.copytree",
                    side_effect=AssertionError("project copied"),
                ),
                mock.patch(
                    "benchmarks.v15_live_e2e.tempfile.mkdtemp",
                    side_effect=AssertionError("workspace created"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                return_code = live_cli.main(
                    [
                        "validate",
                        "--project-root",
                        str(project),
                        "--prompts",
                        str(FIXTURE),
                        "--prompt-limit",
                        "1",
                    ]
                )
            after = tree_snapshot(project)
            result = json.loads(stdout.getvalue())
            self.assertEqual(0, return_code)
            self.assertEqual("valid", result["status"])
            self.assertEqual(
                {
                    "workspace_created": False,
                    "project_copied": False,
                    "report_written": False,
                    "remote_calls": 0,
                },
                result["side_effect_contract"],
            )
            self.assertTrue(
                compare_tree_snapshots(before, after)["unchanged"]
            )

    def test_offline_and_live_commands_select_matching_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            transports: list[str] = []
            smoke_flags: list[bool] = []

            def run_stub(**kwargs: object) -> dict[str, object]:
                transports.append(str(kwargs["transport"]))
                smoke_flags.append(
                    bool(kwargs["include_chat_extraction_smoke"])
                )
                return {
                    "passed": True,
                    "transport": kwargs["transport"],
                    "prompt_count": 1,
                    "measured_round_count": 4,
                    "strict_chain": {"status": "passed"},
                    "chat_extraction_smoke": {
                        "status": (
                            "passed"
                            if kwargs["include_chat_extraction_smoke"]
                            else "not_requested"
                        )
                    },
                    "formal_project_tree": {"unchanged": True},
                }

            with (
                mock.patch.object(
                    live_cli,
                    "run_v15_live_e2e",
                    side_effect=run_stub,
                ),
                mock.patch.object(
                    live_cli,
                    "write_redacted_report",
                    return_value={
                        "output": str(root / "result.json"),
                        "sha256": "0" * 64,
                    },
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                for command in ("offline", "live"):
                    return_code = live_cli.main(
                        [
                            command,
                            "--project-root",
                            str(project),
                            "--prompts",
                            str(FIXTURE),
                            "--prompt-limit",
                            "1",
                            "--output",
                            str(root / f"{command}.json"),
                            *(
                                ["--chat-extraction-smoke"]
                                if command == "live"
                                else []
                            ),
                        ]
                    )
                    self.assertEqual(0, return_code)
            self.assertEqual(["offline", "live"], transports)
            self.assertEqual([False, True], smoke_flags)

    def test_chat_extraction_smoke_flag_requires_live_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                live_cli.main(
                    [
                        "offline",
                        "--project-root",
                        str(project),
                        "--chat-extraction-smoke",
                    ]
                )

    def test_validate_rejects_write_or_workspace_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                live_cli.main(
                    [
                        "validate",
                        "--project-root",
                        str(project),
                        "--output",
                        str(root / "result.json"),
                    ]
                )

    def test_workspace_parent_inside_source_project_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                live_cli.main(
                    [
                        "offline",
                        "--project-root",
                        str(project),
                        "--workspace-parent",
                        str(project / "benchmark-workspaces"),
                    ]
                )


if __name__ == "__main__":
    unittest.main()
