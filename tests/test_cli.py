from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import plot_state as cli  # noqa: E402


def _subcommands(
    parser: argparse.ArgumentParser,
) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _all_parsers(
    parser: argparse.ArgumentParser,
) -> list[argparse.ArgumentParser]:
    result = [parser]
    for child in _subcommands(parser).values():
        result.extend(_all_parsers(child))
    return result


def _make_project(base: Path, *, config_version: int = 3) -> Path:
    root = base / "novel"
    (root / ".plot-rag").mkdir(parents=True)
    config = {
        "config_version": config_version,
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
    }
    (root / ".plot-rag" / "config.json").write_text(
        json.dumps(config, ensure_ascii=False),
        encoding="utf-8",
    )
    return root


class PlotStateCliTestCase(unittest.TestCase):
    def test_explicit_project_root_error_reports_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(
                ValueError,
                "explicitly provided project root",
            ) as raised:
                cli._root(str(root))
        self.assertNotIn("pass --project-root explicitly", str(raised.exception))

    def test_parser_catalog_aliases_and_no_yes_bypass(self) -> None:
        parser = cli._parser()
        top = _subcommands(parser)
        self.assertTrue(
            {
                "prepare",
                "propose",
                "commit",
                "query",
                "query-at",
                "craft",
                "dump",
                "doctor",
                "list-proposals",
                "inspect-proposal",
                "accept-proposal",
                "reject-proposal",
                "retract-proposal",
                "proposal",
                "replay",
                "source-manifest",
                "power-spec",
                "longform",
                "performance",
                "extraction",
                "experience",
                "item",
                "init",
                "migrate",
            }.issubset(top)
        )
        self.assertEqual(
            {"list", "inspect", "accept", "reject", "retract"},
            set(_subcommands(top["proposal"])),
        )
        self.assertEqual(
            {"status", "preview", "propose"},
            set(_subcommands(top["source-manifest"])),
        )
        self.assertEqual(
            {"validate", "preview", "propose"},
            set(_subcommands(top["power-spec"])),
        )
        self.assertEqual(
            {
                "start",
                "dry-run",
                "advance",
                "answer",
                "inspect",
                "propose",
                "apply",
                "verify",
                "list",
                "cancel",
            },
            set(_subcommands(top["init"])),
        )
        self.assertEqual(
            {"refresh", "index", "context", "status", "recover", "benchmark"},
            set(_subcommands(top["longform"])),
        )
        self.assertEqual(
            {"status", "benchmark", "compare"},
            set(_subcommands(top["performance"])),
        )
        self.assertEqual(
            {"list", "inspect", "retry"},
            set(_subcommands(top["extraction"])),
        )
        self.assertEqual(
            {"propose", "inspect", "lock", "review"},
            set(_subcommands(top["experience"])),
        )
        self.assertEqual(
            {
                "definition",
                "instance",
                "inventory",
                "custody",
                "function",
                "runtime",
                "history",
                "observations",
            },
            set(_subcommands(top["item"])),
        )
        for current in _all_parsers(parser):
            options = {
                option
                for action in current._actions
                for option in action.option_strings
            }
            self.assertNotIn("--yes", options)
            self.assertNotIn("--no-materialize", options)

        parsed = parser.parse_args(
            [
                "init",
                "start",
                "--workspace-root",
                "WORKSPACE",
                "--project-root",
                "PROJECT",
                "--mode",
                "new",
                "--seed",
                "玄幻升级流",
                "--idempotency-key",
                "start-1",
            ]
        )
        self.assertEqual("PROJECT", parsed.project_root)
        self.assertEqual("WORKSPACE", parsed.workspace_root)

    def test_source_manifest_parser_contract(self) -> None:
        parser = cli._parser()

        status = parser.parse_args(
            [
                "source-manifest",
                "status",
                "--project-root",
                "PROJECT",
            ]
        )
        preview = parser.parse_args(
            [
                "source-manifest",
                "preview",
                "--project-root",
                "PROJECT",
                "--plan-json",
                '{"schema_version":"plan/v1"}',
                "--expected-canon-revision",
                "7",
            ]
        )
        propose = parser.parse_args(
            [
                "source-manifest",
                "propose",
                "--project-root",
                "PROJECT",
                "--plan",
                "PLAN.json",
                "--expected-canon-revision",
                "8",
                "--idempotency-key",
                "manifest-migration-1",
            ]
        )

        self.assertEqual("source-manifest", status.command)
        self.assertEqual("status", status.source_manifest_command)
        self.assertEqual("PROJECT", status.project_root)
        self.assertEqual("preview", preview.source_manifest_command)
        self.assertEqual('{"schema_version":"plan/v1"}', preview.plan_json)
        self.assertEqual(7, preview.expected_canon_revision)
        self.assertEqual("propose", propose.source_manifest_command)
        self.assertEqual("PLAN.json", propose.plan_json)
        self.assertEqual(8, propose.expected_canon_revision)
        self.assertEqual("manifest-migration-1", propose.idempotency_key)

    def test_power_spec_parser_contract(self) -> None:
        parser = cli._parser()

        validate = parser.parse_args(
            [
                "power-spec",
                "validate",
                "--spec-json",
                '{"schema_version":"plot-rag-power/v1"}',
            ]
        )
        preview = parser.parse_args(
            [
                "power-spec",
                "preview",
                "--project-root",
                "PROJECT",
                "--spec",
                "POWER.json",
                "--expected-canon-revision",
                "7",
            ]
        )
        propose = parser.parse_args(
            [
                "power-spec",
                "propose",
                "--project-root",
                "PROJECT",
                "--spec",
                "-",
                "--expected-canon-revision",
                "8",
                "--idempotency-key",
                "power-spec-import-1",
            ]
        )

        self.assertEqual("validate", validate.power_spec_command)
        self.assertEqual(
            '{"schema_version":"plot-rag-power/v1"}',
            validate.spec_json,
        )
        self.assertEqual("preview", preview.power_spec_command)
        self.assertEqual("POWER.json", preview.spec_json)
        self.assertEqual(7, preview.expected_canon_revision)
        self.assertEqual("propose", propose.power_spec_command)
        self.assertEqual("-", propose.spec_json)
        self.assertEqual(8, propose.expected_canon_revision)
        self.assertEqual("power-spec-import-1", propose.idempotency_key)

    def test_version_distinguishes_plugin_and_runtime_schema(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            cli._parser().parse_args(["--version"])
        self.assertEqual(0, raised.exception.code)
        self.assertEqual(
            "plot-rag-gate 1.6.3 (runtime schema 1)",
            output.getvalue().strip(),
        )

    def test_direct_script_help_loads_v15_runtime_modules(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                str(SCRIPTS / "plot_state.py"),
                "--help",
            ],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            cwd=PLUGIN_ROOT,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("performance", completed.stdout)
        self.assertIn("extraction", completed.stdout)
        self.assertIn("experience", completed.stdout)
        self.assertIn("item", completed.stdout)

    def test_init_start_and_dry_run_work_without_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "blank-novel"

            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(
                    [
                        "init",
                        "dry-run",
                        "--workspace-root",
                        str(workspace),
                        "--project-root",
                        str(project),
                        "--mode",
                        "new",
                        "--seed",
                        "玄幻升级流",
                    ]
                )
            dry = json.loads(output.getvalue())
            self.assertEqual(0, code)
            self.assertFalse(dry["persisted"])
            self.assertFalse(dry["database_touched"])
            self.assertFalse(
                (workspace / ".plot-rag-init" / "init.sqlite3").exists()
            )
            self.assertFalse((project / ".plot-rag").exists())

            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(
                    [
                        "init",
                        "start",
                        "--workspace-root",
                        str(workspace),
                        "--project-root",
                        str(project),
                        "--mode",
                        "new",
                        "--seed",
                        "玄幻升级流",
                        "--idempotency-key",
                        "start-1",
                    ]
                )
            started = json.loads(output.getvalue())
            self.assertEqual(0, code)
            self.assertTrue(started["persisted"])
            self.assertTrue(started["session_id"].startswith("init-"))
            self.assertTrue(
                (workspace / ".plot-rag-init" / "init.sqlite3").is_file()
            )
            self.assertFalse(
                (project / ".plot-rag" / "config.json").exists()
            )

    def test_performance_extraction_and_experience_dispatch(self) -> None:
        root = Path("C:/fixture/project")
        queue = MagicMock()
        queue.list_jobs.return_value = [{"job_id": "job-1"}]
        queue.retry.return_value = {
            "job_id": "job-1",
            "status": "queued",
        }
        experience = MagicMock()
        experience.get_control_revision.return_value = 4
        experience.get_contract.return_value = {
            "contract_id": "contract-1"
        }
        with (
            patch("plot_state._root", return_value=root),
            patch(
                "plot_state.performance_runtime.get_status",
                return_value={"status": "ready"},
            ) as performance_status,
            patch("plot_state.ExtractionJobQueue", return_value=queue),
            patch(
                "plot_state.EventExperienceService.for_project",
                return_value=experience,
            ),
        ):
            status = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "performance",
                        "status",
                        "--project-root",
                        str(root),
                    ]
                )
            )
            listed = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "extraction",
                        "list",
                        "--project-root",
                        str(root),
                        "--status",
                        "failed",
                        "--branch-id",
                        "main",
                    ]
                )
            )
            retried = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "extraction",
                        "retry",
                        "--project-root",
                        str(root),
                        "--job-id",
                        "job-1",
                        "--expected-attempt-count",
                        "2",
                    ]
                )
            )
            inspected = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "experience",
                        "inspect",
                        "--project-root",
                        str(root),
                        "--contract-id",
                        "contract-1",
                    ]
                )
            )

        self.assertEqual("ready", status["status"])
        self.assertEqual(1, listed["count"])
        self.assertEqual("queued", retried["status"])
        self.assertEqual(4, inspected["control_revision"])
        performance_status.assert_called_once_with(root)
        queue.list_jobs.assert_called_once_with(
            status=["failed"],
            branch_id="main",
            sequence_no=None,
            receipt_id=None,
            limit=100,
            offset=0,
        )
        queue.retry.assert_called_once_with(
            "job-1",
            expected_attempt_count=2,
            next_attempt_at=None,
        )
        experience.get_contract.assert_called_once_with("contract-1")

    def test_performance_compare_and_item_custody_alias_dispatch(self) -> None:
        root = Path("C:/fixture/project")
        compared = {"status": "compared"}
        calls: list[tuple[str, str]] = []

        class ItemService:
            def query_item_custody(
                self,
                *,
                subject_type: str,
                subject_id: str,
            ) -> dict[str, Any]:
                calls.append((subject_type, subject_id))
                return {"status": "ready", "custody": {}}

        with patch(
            "plot_state.performance_runtime.compare_reports",
            return_value=compared,
        ) as compare:
            result = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "performance",
                        "compare",
                        "--left",
                        '{"telemetry":{"prepare":{"p50_ms":10}}}',
                        "--right",
                        '{"telemetry":{"prepare":{"p50_ms":8}}}',
                    ]
                )
            )
        with (
            patch("plot_state._root", return_value=root),
            patch("plot_state.ContinuityService", return_value=ItemService()),
        ):
            custody = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "item",
                        "custody",
                        "--project-root",
                        str(root),
                        "--subject-type",
                        "instance",
                        "--subject-id",
                        "item-instance-1",
                    ]
                )
            )

        self.assertEqual(compared, result)
        compare.assert_called_once()
        self.assertEqual("ready", custody["status"])
        self.assertEqual(
            [("item_instance", "item-instance-1")],
            calls,
        )

    def test_item_history_dispatch_uses_real_v6_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _make_project(Path(temporary))
            cli.ContinuityService(root)
            result = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "item",
                        "history",
                        "--project-root",
                        str(root),
                    ]
                )
            )
        self.assertEqual([], result["history"])
        self.assertEqual(1, result["item_projection_schema_version"])
        self.assertRegex(
            result["item_projection_hash"],
            r"^item_projection_[0-9a-f]{64}$",
        )

    def test_proposal_list_inspect_and_reject_dispatch(self) -> None:
        root = Path("C:/fixture/project")
        service = MagicMock()
        service.list_proposals.return_value = [{"proposal_id": "proposal-1"}]
        service.get_canon_revisions.return_value = {"head": 2, "active": 1}
        service.inspect_proposal.return_value = {"proposal_id": "proposal-1"}
        with (
            patch("plot_state._root", return_value=root),
            patch("plot_state.ContinuityService", return_value=service),
            patch(
                "plot_state.v1.reject_plot_proposal",
                return_value={"status": "rejected"},
            ) as reject,
        ):
            listed = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "proposal",
                        "list",
                        "--project-root",
                        str(root),
                        "--canon-status",
                        "proposed",
                        "--branch-id",
                        "main",
                    ]
                )
            )
            inspected = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "inspect-proposal",
                        "--project-root",
                        str(root),
                        "--proposal-id",
                        "proposal-1",
                    ]
                )
            )
            rejected = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "proposal",
                        "reject",
                        "--project-root",
                        str(root),
                        "--proposal-id",
                        "proposal-1",
                        "--reason",
                        "conflict",
                        "--idempotency-key",
                        "reject-1",
                    ]
                )
            )

        self.assertEqual(1, listed["count"])
        self.assertEqual("proposal-1", inspected["proposal"]["proposal_id"])
        self.assertEqual("rejected", rejected["status"])
        service.list_proposals.assert_called_once_with(
            canon_status="proposed",
            branch_id="main",
        )
        reject.assert_called_once_with(
            root,
            "proposal-1",
            reason="conflict",
            idempotency_key="reject-1",
        )

    def test_source_manifest_status_preview_and_propose_dispatch(self) -> None:
        root = Path("C:/fixture/project")
        plan = {
            "schema_version": "plot-rag-source-manifest-migration-plan/v1",
            "operations": {
                "deactivate_entry_ids": ["manifest-entry-old"],
                "retain_entry_ids": [],
                "upserts": [],
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            plan_path = Path(temporary) / "source-manifest-plan.json"
            plan_path.write_text(
                json.dumps(plan, ensure_ascii=False),
                encoding="utf-8",
            )
            with (
                patch("plot_state._root", return_value=root),
                patch(
                    "plot_state.v1.source_manifest_status",
                    return_value={"status": "ready"},
                ) as status,
                patch(
                    "plot_state.v1.preview_source_manifest_change",
                    return_value={"status": "previewed"},
                ) as preview,
                patch(
                    "plot_state.v1.propose_source_manifest_change",
                    return_value={"status": "proposed"},
                ) as propose,
            ):
                status_result = cli._dispatch(
                    cli._parser().parse_args(
                        [
                            "source-manifest",
                            "status",
                            "--project-root",
                            str(root),
                        ]
                    )
                )
                preview_result = cli._dispatch(
                    cli._parser().parse_args(
                        [
                            "source-manifest",
                            "preview",
                            "--project-root",
                            str(root),
                            "--plan-json",
                            json.dumps(plan, ensure_ascii=False),
                            "--expected-canon-revision",
                            "7",
                        ]
                    )
                )
                propose_result = cli._dispatch(
                    cli._parser().parse_args(
                        [
                            "source-manifest",
                            "propose",
                            "--project-root",
                            str(root),
                            "--plan",
                            str(plan_path),
                            "--expected-canon-revision",
                            "8",
                            "--idempotency-key",
                            "manifest-migration-1",
                        ]
                    )
                )

        self.assertEqual("ready", status_result["status"])
        self.assertEqual("previewed", preview_result["status"])
        self.assertEqual("proposed", propose_result["status"])
        status.assert_called_once_with(root)
        preview.assert_called_once_with(
            root,
            plan,
            expected_canon_revision=7,
        )
        propose.assert_called_once_with(
            root,
            plan,
            expected_canon_revision=8,
            idempotency_key="manifest-migration-1",
        )

    def test_power_spec_validate_preview_and_propose_dispatch_inputs(
        self,
    ) -> None:
        root = Path("C:/fixture/project")
        power_spec = {
            "schema_version": "plot-rag-power/v1",
            "power_systems": [{"name": "修行体系"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            spec_path = Path(temporary) / "power-spec.json"
            spec_path.write_text(
                json.dumps(power_spec, ensure_ascii=False),
                encoding="utf-8",
            )
            with (
                patch("plot_state._root", return_value=root) as resolve_root,
                patch(
                    "plot_state.v1.validate_power_spec_change",
                    return_value={"status": "ready"},
                ) as validate,
                patch(
                    "plot_state.v1.preview_power_spec_change",
                    return_value={"status": "ready"},
                ) as preview,
                patch(
                    "plot_state.v1.propose_power_spec_change",
                    return_value={"status": "proposed"},
                ) as propose,
            ):
                validate_result = cli._dispatch(
                    cli._parser().parse_args(
                        [
                            "power-spec",
                            "validate",
                            "--spec-json",
                            json.dumps(power_spec, ensure_ascii=False),
                        ]
                    )
                )
                preview_result = cli._dispatch(
                    cli._parser().parse_args(
                        [
                            "power-spec",
                            "preview",
                            "--project-root",
                            str(root),
                            "--spec",
                            str(spec_path),
                            "--expected-canon-revision",
                            "7",
                        ]
                    )
                )
                with patch(
                    "plot_state.sys.stdin",
                    io.StringIO(json.dumps(power_spec, ensure_ascii=False)),
                ):
                    propose_result = cli._dispatch(
                        cli._parser().parse_args(
                            [
                                "power-spec",
                                "propose",
                                "--project-root",
                                str(root),
                                "--spec",
                                "-",
                                "--expected-canon-revision",
                                "8",
                                "--idempotency-key",
                                "power-spec-import-1",
                            ]
                        )
                    )

        self.assertEqual("ready", validate_result["status"])
        self.assertEqual("ready", preview_result["status"])
        self.assertEqual("proposed", propose_result["status"])
        validate.assert_called_once_with(power_spec)
        preview.assert_called_once_with(
            root,
            power_spec,
            expected_canon_revision=7,
        )
        propose.assert_called_once_with(
            root,
            power_spec,
            expected_canon_revision=8,
            idempotency_key="power-spec-import-1",
        )
        self.assertEqual(2, resolve_root.call_count)

    def test_power_spec_json_input_is_strict(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            cli._dispatch(
                cli._parser().parse_args(
                    [
                        "power-spec",
                        "validate",
                        "--spec",
                        "{'schema_version':'plot-rag-power/v1'}",
                    ]
                )
            )

    def test_non_tty_without_approval_never_issues_or_mutates(self) -> None:
        root = Path("C:/fixture/project")
        workspace = Path("C:/fixture")
        fake_initializer = MagicMock()
        parser = cli._parser()
        cases = [
            (
                [
                    "accept-proposal",
                    "--project-root",
                    str(root),
                    "--proposal-id",
                    "proposal-1",
                    "--expected-canon-revision",
                    "3",
                ],
                "accept_plot_proposal",
            ),
            (
                [
                    "proposal",
                    "retract",
                    "--project-root",
                    str(root),
                    "--proposal-id",
                    "proposal-1",
                    "--expected-canon-revision",
                    "3",
                    "--reason",
                    "rewrite",
                ],
                "retract_plot_proposal",
            ),
            (
                [
                    "init",
                    "apply",
                    "--workspace-root",
                    str(workspace),
                    "--project-root",
                    str(root),
                    "--proposal-id",
                    "init-proposal-1",
                    "--expected-canon-revision",
                    "3",
                    "--idempotency-key",
                    "apply-1",
                ],
                "apply_initialization_proposal",
            ),
        ]
        for argv, operation in cases:
            with self.subTest(operation=operation):
                with (
                    patch("plot_state._root", return_value=root),
                    patch(
                        "plot_state.v1.init_service",
                        return_value=fake_initializer,
                    ),
                    patch("plot_state.sys.stdin.isatty", return_value=False),
                    patch("plot_state.v1.issue_host_approval") as issue,
                    patch(
                        "plot_state.v1.prepare_initialization_apply",
                        return_value={"status": "ready"},
                    ),
                    patch(f"plot_state.v1.{operation}") as mutate,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "interactive TTY",
                    ):
                        cli._dispatch(parser.parse_args(argv))
                issue.assert_not_called()
                mutate.assert_not_called()

    def test_tty_double_confirmation_issues_then_accepts(self) -> None:
        root = Path("C:/fixture/project")
        args = cli._parser().parse_args(
            [
                "proposal",
                "accept",
                "--project-root",
                str(root),
                "--proposal-id",
                "proposal-1",
                "--expected-canon-revision",
                "7",
            ]
        )
        with (
            patch("plot_state._root", return_value=root),
            patch("plot_state.sys.stdin.isatty", return_value=True),
            patch(
                "builtins.input",
                side_effect=["proposal-1", "proposal-1"],
            ),
            patch("plot_state.getpass.getuser", return_value="tester"),
            patch(
                "plot_state.v1.issue_host_approval",
                return_value={
                    "grant": {"approval_id": "approval-1"}
                },
            ) as issue,
            patch(
                "plot_state.v1.accept_plot_proposal",
                return_value={"status": "accepted"},
            ) as accept,
        ):
            result = cli._dispatch(args)

        self.assertEqual("accepted", result["status"])
        issue.assert_called_once_with(
            root,
            "proposal-1",
            expected_canon_revision=7,
            issuer="local-cli:tester",
            channel="interactive_cli",
            operations=None,
            workspace_root=None,
        )
        accept.assert_called_once_with(
            root,
            "proposal-1",
            approval_id="approval-1",
            expected_canon_revision=7,
            workspace_root=None,
        )

    def test_confirmation_mismatch_blocks_accept_retract_and_init_apply(self) -> None:
        root = Path("C:/fixture/project")
        workspace = Path("C:/fixture")
        fake_initializer = MagicMock()
        cases = [
            (
                [
                    "accept-proposal",
                    "--project-root",
                    str(root),
                    "--proposal-id",
                    "proposal-1",
                    "--expected-canon-revision",
                    "1",
                ],
                "accept_plot_proposal",
            ),
            (
                [
                    "retract-proposal",
                    "--project-root",
                    str(root),
                    "--proposal-id",
                    "proposal-1",
                    "--expected-canon-revision",
                    "1",
                    "--reason",
                    "rewrite",
                ],
                "retract_plot_proposal",
            ),
            (
                [
                    "init",
                    "apply",
                    "--workspace-root",
                    str(workspace),
                    "--project-root",
                    str(root),
                    "--proposal-id",
                    "init-proposal-1",
                    "--expected-canon-revision",
                    "1",
                    "--idempotency-key",
                    "apply-1",
                ],
                "apply_initialization_proposal",
            ),
        ]
        for argv, operation in cases:
            with self.subTest(operation=operation):
                with (
                    patch("plot_state._root", return_value=root),
                    patch(
                        "plot_state.v1.init_service",
                        return_value=fake_initializer,
                    ),
                    patch("plot_state.sys.stdin.isatty", return_value=True),
                    patch(
                        "builtins.input",
                        side_effect=["proposal-1", "different"],
                    ),
                    patch("plot_state.v1.issue_host_approval") as issue,
                    patch(
                        "plot_state.v1.prepare_initialization_apply",
                        return_value={"status": "ready"},
                    ),
                    patch(f"plot_state.v1.{operation}") as mutate,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "confirmation mismatch",
                    ):
                        cli._dispatch(cli._parser().parse_args(argv))
                issue.assert_not_called()
                mutate.assert_not_called()

    def test_existing_approval_skips_issuance_for_all_mutations(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name).resolve(strict=False)
        root = (workspace / "project").resolve(strict=False)
        root.mkdir()
        fake_initializer = MagicMock()
        with (
            patch("plot_state._root", return_value=root),
            patch(
                "plot_state.v1.init_service",
                return_value=fake_initializer,
            ),
            patch("plot_state.v1.issue_host_approval") as issue,
            patch(
                "plot_state.v1.accept_plot_proposal",
                return_value={"status": "accepted"},
            ) as accept,
            patch(
                "plot_state.v1.retract_plot_proposal",
                return_value={"status": "retracted"},
            ) as retract,
            patch(
                "plot_state.v1.apply_initialization_proposal",
                return_value={"status": "completed"},
            ) as apply,
            patch(
                "plot_state.v1.prepare_initialization_apply",
                return_value={"status": "ready"},
            ),
        ):
            cli._dispatch(
                cli._parser().parse_args(
                    [
                        "accept-proposal",
                        "--project-root",
                        str(root),
                        "--proposal-id",
                        "proposal-1",
                        "--approval-id",
                        "approval-existing",
                        "--expected-canon-revision",
                        "2",
                    ]
                )
            )
            cli._dispatch(
                cli._parser().parse_args(
                    [
                        "retract-proposal",
                        "--project-root",
                        str(root),
                        "--proposal-id",
                        "proposal-1",
                        "--approval-id",
                        "approval-existing",
                        "--expected-canon-revision",
                        "2",
                        "--reason",
                        "rewrite",
                    ]
                )
            )
            cli._dispatch(
                cli._parser().parse_args(
                    [
                        "init",
                        "apply",
                        "--workspace-root",
                        str(workspace),
                        "--project-root",
                        str(root),
                        "--proposal-id",
                        "init-proposal-1",
                        "--approval-id",
                        "approval-existing",
                        "--expected-canon-revision",
                        "2",
                        "--idempotency-key",
                        "apply-1",
                    ]
                )
            )

        issue.assert_not_called()
        accept.assert_called_once_with(
            root,
            "proposal-1",
            approval_id="approval-existing",
            expected_canon_revision=2,
            workspace_root=None,
        )
        retract.assert_called_once_with(
            root,
            "proposal-1",
            approval_id="approval-existing",
            expected_canon_revision=2,
            reason="rewrite",
        )
        apply.assert_called_once_with(
            root,
            "init-proposal-1",
            approval_id="approval-existing",
            expected_canon_revision=2,
            idempotency_key="apply-1",
            workspace_root=workspace,
            materialize=True,
        )

    def test_tty_init_apply_binds_materialize_operations(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name).resolve(strict=False)
        root = (workspace / "project").resolve(strict=False)
        root.mkdir()
        fake_initializer = MagicMock()
        args = cli._parser().parse_args(
            [
                "init",
                "apply",
                "--workspace-root",
                str(workspace),
                "--project-root",
                str(root),
                "--proposal-id",
                "init-proposal-1",
                "--expected-canon-revision",
                "4",
                "--idempotency-key",
                "apply-1",
            ]
        )
        with (
            patch(
                "plot_state.v1.init_service",
                return_value=fake_initializer,
            ),
            patch("plot_state.sys.stdin.isatty", return_value=True),
            patch(
                "builtins.input",
                side_effect=["init-proposal-1", "init-proposal-1"],
            ),
            patch("plot_state.getpass.getuser", return_value="tester"),
            patch(
                "plot_state.v1.issue_host_approval",
                return_value={
                    "grant": {"approval_id": "approval-init"}
                },
            ) as issue,
            patch(
                "plot_state.v1.apply_initialization_proposal",
                return_value={"status": "completed"},
            ) as apply,
            patch(
                "plot_state.v1.prepare_initialization_apply",
                return_value={
                    "status": "ready",
                    "expected_canon_revision": 4,
                },
            ) as prepare,
        ):
            result = cli._dispatch(args)

        self.assertEqual("completed", result["status"])
        prepare.assert_called_once_with(
            root,
            "init-proposal-1",
            workspace_root=workspace,
        )
        issue.assert_called_once_with(
            root,
            "init-proposal-1",
            expected_canon_revision=4,
            issuer="local-cli:tester",
            channel="interactive_cli",
            operations=("accept_initialization", "materialize"),
            workspace_root=workspace,
        )
        apply.assert_called_once_with(
            root,
            "init-proposal-1",
            approval_id="approval-init",
            expected_canon_revision=4,
            idempotency_key="apply-1",
            workspace_root=workspace,
            materialize=True,
        )

    def test_init_apply_can_resolve_target_from_frozen_proposal(self) -> None:
        workspace = Path("C:/fixture")
        target = workspace / "novel"
        fake_initializer = MagicMock()
        fake_initializer.storage.load_proposal.return_value = {
            "bundle": {
                "target_project_real_path": str(target),
            }
        }
        with (
            patch(
                "plot_state.v1.init_service",
                return_value=fake_initializer,
            ),
            patch(
                "plot_state.v1.apply_initialization_proposal",
                return_value={"status": "completed"},
            ) as apply,
            patch(
                "plot_state.v1.prepare_initialization_apply",
                return_value={"status": "ready"},
            ),
        ):
            result = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "init",
                        "apply",
                        "--workspace-root",
                        str(workspace),
                        "--proposal-id",
                        "init-proposal-1",
                        "--approval-id",
                        "approval-existing",
                        "--expected-canon-revision",
                        "0",
                        "--idempotency-key",
                        "apply-1",
                    ]
                )
            )

        self.assertEqual("completed", result["status"])
        apply.assert_called_once_with(
            target.resolve(strict=False),
            "init-proposal-1",
            approval_id="approval-existing",
            expected_canon_revision=0,
            idempotency_key="apply-1",
            workspace_root=workspace.resolve(strict=False),
            materialize=True,
        )

    def test_init_apply_first_stage_returns_without_signing_grant(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name).resolve(strict=False)
        root = (workspace / "project").resolve(strict=False)
        root.mkdir()
        fake_initializer = MagicMock()
        expected = {
            "status": "POWER_SPEC_APPROVAL_REQUIRED",
            "power_spec_proposal_id": "proposal-power",
            "expected_canon_revision": 0,
        }
        with (
            patch("plot_state._root", return_value=root),
            patch(
                "plot_state.v1.init_service",
                return_value=fake_initializer,
            ),
            patch(
                "plot_state.v1.prepare_initialization_apply",
                return_value=expected,
            ) as prepare,
            patch("plot_state.v1.issue_host_approval") as issue,
            patch(
                "plot_state.v1.apply_initialization_proposal"
            ) as apply,
        ):
            result = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "init",
                        "apply",
                        "--workspace-root",
                        str(workspace),
                        "--project-root",
                        str(root),
                        "--proposal-id",
                        "init-proposal-1",
                        "--expected-canon-revision",
                        "0",
                        "--idempotency-key",
                        "apply-first-stage",
                    ]
                )
            )

        self.assertEqual(expected, result)
        prepare.assert_called_once_with(
            root,
            "init-proposal-1",
            workspace_root=workspace,
        )
        issue.assert_not_called()
        apply.assert_not_called()

    def test_init_answer_reads_json_object_from_stdin(self) -> None:
        workspace = Path("C:/fixture")
        fake_initializer = MagicMock()
        fake_initializer.answer.return_value = {"status": "ready"}
        stream = io.StringIO('{"genre": "玄幻"}')
        with (
            patch(
                "plot_state.v1.init_service",
                return_value=fake_initializer,
            ),
            patch("plot_state.sys.stdin", stream),
        ):
            result = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "init",
                        "answer",
                        "--workspace-root",
                        str(workspace),
                        "--session-id",
                        "init-1",
                        "--answers-file",
                        "-",
                        "--expected-session-revision",
                        "3",
                        "--idempotency-key",
                        "answer-1",
                    ]
                )
            )
        self.assertEqual("ready", result["status"])
        fake_initializer.answer.assert_called_once_with(
            "init-1",
            {"genre": "玄幻"},
            expected_session_revision=3,
            idempotency_key="answer-1",
        )

    def test_query_at_maps_all_temporal_and_branch_filters(self) -> None:
        root = Path("C:/fixture/project")
        with (
            patch("plot_state._root", return_value=root),
            patch("plot_state.v1.is_strict_lifecycle", return_value=True),
            patch(
                "plot_state.v1.query_continuity",
                return_value={"status": "ready"},
            ) as query,
        ):
            result = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "query-at",
                        "--project-root",
                        str(root),
                        "--mention",
                        "测试角色甲",
                        "--entity-id",
                        "char-1",
                        "--fact-type",
                        "location",
                        "--scope",
                        "historical",
                        "--chapter-no",
                        "12",
                        "--scene-index",
                        "3",
                        "--branch-id",
                        "alt-a",
                        "--include-historical",
                        "--include-provisional",
                        "--exclude-relations",
                    ]
                )
            )
        self.assertEqual("ready", result["status"])
        query.assert_called_once_with(
            root,
            mention="测试角色甲",
            entity_id="char-1",
            fact_type="location",
            scope="historical",
            chapter_no=12,
            scene_index=3,
            branch_id="alt-a",
            include_historical=True,
            include_provisional=True,
            include_relations=False,
        )

    def test_longform_commands_map_and_benchmark_needs_no_project(self) -> None:
        root = Path("C:/fixture/project")
        manifest = Path("C:/fixture/annotations.jsonl")
        with (
            patch("plot_state._root", return_value=root) as resolve_root,
            patch(
                "plot_state.v1.refresh_longform_index",
                return_value={"status": "ready"},
            ) as refresh,
            patch(
                "plot_state.v1.infer_artifact_context",
                return_value={"artifact_stage": "outline"},
            ) as infer,
            patch(
                "plot_state.v1.build_longform_context",
                return_value={"status": "ready"},
            ) as context,
            patch(
                "plot_state.v1.longform_status",
                return_value={"status": "ready"},
            ) as status,
            patch(
                "plot_state.v1.run_longform_benchmark",
                return_value={"status": "passed"},
            ) as benchmark,
        ):
            cli._dispatch(
                cli._parser().parse_args(
                    [
                        "longform",
                        "index",
                        "--project-root",
                        str(root),
                        "--with-embeddings",
                    ]
                )
            )
            cli._dispatch(
                cli._parser().parse_args(
                    [
                        "longform",
                        "context",
                        "--project-root",
                        str(root),
                        "--prompt",
                        "设计第十二章章纲",
                        "--artifact-stage",
                        "outline",
                        "--chapter-no",
                        "12",
                        "--max-context-chars",
                        "9000",
                    ]
                )
            )
            cli._dispatch(
                cli._parser().parse_args(
                    [
                        "longform",
                        "status",
                        "--project-root",
                        str(root),
                    ]
                )
            )
            root_calls_before_benchmark = resolve_root.call_count
            result = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "longform",
                        "benchmark",
                        "--manifest",
                        str(manifest),
                    ]
                )
            )

        self.assertEqual("passed", result["status"])
        refresh.assert_called_once_with(root, with_embeddings=True)
        infer.assert_called_once_with(
            "设计第十二章章纲",
            artifact_stage="outline",
            branch_id=None,
            chapter_no=12,
            scene_index=None,
            artifact_id=None,
            task=None,
        )
        context.assert_called_once_with(
            root,
            "设计第十二章章纲",
            artifact_context={"artifact_stage": "outline"},
            max_context_chars=9000,
        )
        status.assert_called_once_with(root)
        self.assertEqual(root_calls_before_benchmark, resolve_root.call_count)
        benchmark.assert_called_once_with(manifest.resolve(strict=False))

    def test_v3_compat_query_and_dump_use_accepted_continuity(self) -> None:
        root = Path("C:/fixture/project")
        state_path = Path("C:/fixture/state.sqlite3")
        with (
            patch("plot_state._root", return_value=root),
            patch("plot_state.v1.is_strict_lifecycle", return_value=True),
            patch(
                "plot_state.load_config",
                return_value={"state": {"db_path": str(state_path)}},
            ),
            patch.object(Path, "is_file", return_value=True),
            patch(
                "plot_state.v1.query_continuity_text",
                side_effect=[
                    {"status": "ready", "kind": "query"},
                    {"status": "ready", "kind": "dump"},
                ],
            ) as continuity,
            patch("plot_state.query_state") as legacy_query,
            patch("plot_state.dump_state") as legacy_dump,
        ):
            queried = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "query",
                        "--project-root",
                        str(root),
                        "--query",
                        "测试角色甲在哪里",
                        "--category",
                        "location",
                        "--top-k",
                        "8",
                    ]
                )
            )
            dumped = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "dump",
                        "--project-root",
                        str(root),
                        "--subject",
                        "测试角色甲",
                        "--category",
                        "location",
                    ]
                )
            )

        self.assertEqual("query", queried["kind"])
        self.assertEqual("dump", dumped["kind"])
        self.assertEqual(
            [
                call(
                    root,
                    "测试角色甲在哪里",
                    categories=["location"],
                    top_k=8,
                    include_historical=False,
                ),
                call(
                    root,
                    "测试角色甲",
                    subject="测试角色甲",
                    category="location",
                    top_k=200,
                    include_historical=True,
                ),
            ],
            continuity.call_args_list,
        )
        legacy_query.assert_not_called()
        legacy_dump.assert_not_called()

    def test_legacy_query_and_dump_keep_legacy_projection(self) -> None:
        root = Path("C:/fixture/project")
        with (
            patch("plot_state._root", return_value=root),
            patch("plot_state.v1.is_strict_lifecycle", return_value=False),
            patch(
                "plot_state.query_state",
                return_value={"status": "ready", "kind": "query"},
            ) as query,
            patch(
                "plot_state.dump_state",
                return_value={"status": "ready", "kind": "dump"},
            ) as dump,
            patch("plot_state.v1.query_continuity_text") as continuity,
        ):
            queried = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "query",
                        "--project-root",
                        str(root),
                        "--query",
                        "测试角色甲",
                    ]
                )
            )
            dumped = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "dump",
                        "--project-root",
                        str(root),
                        "--subject",
                        "测试角色甲",
                    ]
                )
            )

        self.assertEqual("query", queried["kind"])
        self.assertEqual("dump", dumped["kind"])
        query.assert_called_once_with(
            root,
            "测试角色甲",
            categories=None,
            top_k=None,
        )
        dump.assert_called_once_with(root, subject="测试角色甲", category=None)
        continuity.assert_not_called()

    def test_migrate_command_maps_component_and_dry_run(self) -> None:
        root = Path("C:/fixture/project")
        with (
            patch("plot_state._root", return_value=root),
            patch(
                "plot_state.v1.migrate_project",
                return_value={"status": "dry_run"},
            ) as migrate,
        ):
            result = cli._dispatch(
                cli._parser().parse_args(
                    [
                        "migrate",
                        "--project-root",
                        str(root),
                        "--component",
                        "state",
                        "--dry-run",
                    ]
                )
            )
        self.assertEqual("dry_run", result["status"])
        migrate.assert_called_once_with(
            root,
            component="state",
            dry_run=True,
        )


if __name__ == "__main__":
    unittest.main()
