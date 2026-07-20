from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import plot_rag_mcp as mcp  # noqa: E402
import plot_state as cli  # noqa: E402


ADVANTAGE_TOOLS = {
    "query_advantage_definition",
    "query_advantage_anchors",
    "query_advantage_runtime",
    "query_advantage_modules",
    "query_advantage_ledger",
    "query_advantage_knowledge",
    "query_advantage_progression",
    "query_advantage_exposure",
    "query_special_item_context",
}


def _subcommands(
    parser: argparse.ArgumentParser,
) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _make_project(base: Path) -> Path:
    root = base / "novel"
    (root / ".plot-rag").mkdir(parents=True)
    (root / ".plot-rag" / "config.json").write_text(
        json.dumps(
            {
                "config_version": 3,
                "enabled": True,
                "remote": {
                    "embedding": {"enabled": False},
                    "rerank": {"enabled": False},
                    "extract": {"enabled": False},
                },
            }
        ),
        encoding="utf-8",
    )
    return root


class _Store:
    def __init__(self, *, readonly_failure: bool = False) -> None:
        self.connection = object()
        self.ensure_count = 0
        self.read_count = 0
        self.transaction_count = 0
        self.readonly_failure = readonly_failure

    def ensure_schema(self) -> None:
        self.ensure_count += 1

    @contextmanager
    def read_connection(self):
        self.read_count += 1
        if self.readonly_failure:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        yield self.connection

    @contextmanager
    def transaction(self):
        self.transaction_count += 1
        yield self.connection


class _Service:
    stores: list[_Store] = []
    readonly_failure = False

    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = _Store(readonly_failure=self.readonly_failure)
        self.stores.append(self.store)


class _ItemObservationService:
    calls: list[dict[str, Any]] = []

    def __init__(self, root: Path) -> None:
        self.root = root

    def query_item_observations(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        visibility = str(kwargs.get("visibility") or "generation")
        rows = [
            {
                "knowledge_plane": "public_narrative",
                "observer_entity_id": None,
                "observation": "众人看见遗物发光。",
            },
            {
                "knowledge_plane": "actor_belief",
                "observer_entity_id": "actor-owner",
                "observation": "持有者认为它正在苏醒。",
            },
            {
                "knowledge_plane": "actor_belief",
                "observer_entity_id": "actor-other",
                "observation": "旁观者误判了来源。",
            },
            {
                "knowledge_plane": "author_plan",
                "observer_entity_id": None,
                "observation": "终局才揭示真实代价。",
            },
        ]
        if visibility == "generation":
            observer = str(kwargs.get("observer_entity_id") or "")
            rows = [
                row
                for row in rows
                if row["knowledge_plane"] != "author_plan"
                and (
                    row["knowledge_plane"] != "actor_belief"
                    or (
                        observer
                        and row["observer_entity_id"] == observer
                    )
                )
            ]
        return {"visibility": visibility, "observations": rows}


class _Queries:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, str, dict[str, Any]]] = []

    def _record(
        self,
        name: str,
        connection: Any,
        advantage_id: str,
        kwargs: dict[str, Any],
        result: Any,
    ) -> Any:
        self.calls.append((name, connection, advantage_id, kwargs))
        return result

    def query_advantage_definition(
        self,
        connection: Any,
        advantage_id: str,
    ) -> dict[str, Any]:
        return self._record(
            "definition",
            connection,
            advantage_id,
            {},
            {
                "advantage_id": advantage_id,
                "title": "样例优势核心",
                "advantage_status": "canon",
                "lifecycle_status": "active",
                "source_event_id": "event-control-only",
                "definition_json": {
                    "author_plan": "终局揭示",
                    "public_fact": "可识别异常样本",
                },
            },
        )

    def query_advantage_runtime(
        self,
        connection: Any,
        advantage_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self._record(
            "runtime",
            connection,
            advantage_id,
            kwargs,
            {"stage": "状态解析"},
        )

    def query_advantage_anchors(
        self,
        connection: Any,
        advantage_id: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        return self._record(
            "anchors",
            connection,
            advantage_id,
            kwargs,
            [
                {
                    "anchor_id": "anchor-sample-core",
                    "anchor_type": "item_instance",
                    "binding_state": "bound",
                    "authority_status": "canon",
                    "source_event_id": "event-control-only",
                }
            ],
        )

    def query_advantage_modules(
        self,
        connection: Any,
        advantage_id: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        return self._record(
            "modules",
            connection,
            advantage_id,
            kwargs,
            [{"module_id": "状态解析"}],
        )

    def query_advantage_ledger(
        self,
        connection: Any,
        advantage_id: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        return self._record(
            "ledger",
            connection,
            advantage_id,
            kwargs,
            [{"entry_id": "ledger-1"}],
        )

    def query_advantage_knowledge(
        self,
        connection: Any,
        advantage_id: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        return self._record(
            "knowledge",
            connection,
            advantage_id,
            kwargs,
            [
                {
                    "knowledge_plane": "reader_disclosed",
                    "claim": "可识别异常样本",
                },
                {
                    "knowledge_plane": "author_plan",
                    "claim": "终局才揭示真实来源",
                    "control_metadata": {"phase": "finale"},
                },
            ],
        )

    def query_advantage_progression(
        self,
        connection: Any,
        advantage_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self._record(
            "progression",
            connection,
            advantage_id,
            kwargs,
            {"current_stage": "状态解析"},
        )

    def query_advantage_exposure(
        self,
        connection: Any,
        advantage_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self._record(
            "exposure",
            connection,
            advantage_id,
            kwargs,
            {"exposure": 2},
        )

    def query_advantage_context(
        self,
        connection: Any,
        advantage_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self._record(
            "context",
            connection,
            advantage_id,
            kwargs,
            {
                "definition": {
                    "title": "样例优势核心",
                    "advantage_status": "canon",
                    "lifecycle_status": "active",
                },
                "modules": [{"module_id": "状态解析"}],
                "runtime": {"stage": "状态解析"},
                "ledger": [],
                "knowledge": [],
                "progression": {"current_stage": "状态解析"},
                "exposure": {"exposure": 2},
            },
        )


class AdvantageCliMcpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _Service.stores.clear()
        _Service.readonly_failure = False
        _ItemObservationService.calls.clear()

    def test_cli_catalog_exposes_advantage_and_special_item_queries(self) -> None:
        top = _subcommands(cli._parser())
        self.assertIn("advantage", top)
        self.assertIn("special-item", top)
        self.assertIn("special-item-context", top)
        self.assertEqual(
            {
                "definition",
                "anchors",
                "anchor",
                "runtime",
                "modules",
                "module",
                "ledger",
                "knowledge",
                "progression",
                "exposure",
            },
            set(_subcommands(top["advantage"])),
        )
        self.assertEqual(
            {"context", "inventory"},
            set(_subcommands(top["special-item"])),
        )

    def test_cli_modules_forwards_filters_and_wraps_list_payload(self) -> None:
        queries = _Queries()
        with tempfile.TemporaryDirectory() as temporary:
            root = _make_project(Path(temporary))
            output = io.StringIO()
            with (
                patch("plot_state.ContinuityService", _Service),
                patch("plot_state._load_advantage_queries", return_value=queries),
                redirect_stdout(output),
            ):
                code = cli.main(
                    [
                        "advantage",
                        "modules",
                        "--project-root",
                        str(root),
                        "--advantage-id",
                        "advantage-sample-core",
                        "--enabled-only",
                    ]
                )

        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertEqual("ready", payload["status"])
        self.assertEqual("advantage-sample-core", payload["advantage_id"])
        self.assertEqual(1, payload["count"])
        self.assertEqual("状态解析", payload["modules"][0]["module_id"])
        self.assertEqual(
            ("modules", _Service.stores[0].connection, "advantage-sample-core"),
            queries.calls[-1][:3],
        )
        self.assertEqual({"enabled_only": True}, queries.calls[-1][3])
        self.assertEqual("definition", queries.calls[0][0])
        self.assertEqual(1, _Service.stores[0].read_count)
        self.assertEqual(0, _Service.stores[0].transaction_count)

    def test_cli_anchors_locks_generation_and_allows_explicit_inspection(
        self,
    ) -> None:
        queries = _Queries()
        with tempfile.TemporaryDirectory() as temporary:
            root = _make_project(Path(temporary))
            generation_args = cli._parser().parse_args(
                [
                    "advantage",
                    "anchor",
                    "--project-root",
                    str(root),
                    "--advantage-id",
                    "advantage-sample-core",
                    "--include-inactive",
                    "--include-noncanon",
                ]
            )
            inspection_args = cli._parser().parse_args(
                [
                    "advantage",
                    "anchors",
                    "--project-root",
                    str(root),
                    "--advantage-id",
                    "advantage-sample-core",
                    "--include-inactive",
                    "--include-noncanon",
                    "--visibility",
                    "inspection",
                ]
            )
            with (
                patch("plot_state.ContinuityService", _Service),
                patch("plot_state._load_advantage_queries", return_value=queries),
            ):
                generation = cli._dispatch(generation_args)
                inspection = cli._dispatch(inspection_args)

        self.assertEqual("generation", generation["visibility"])
        self.assertEqual(1, generation["count"])
        self.assertNotIn("authority_status", generation["anchors"][0])
        self.assertNotIn("source_event_id", generation["anchors"][0])
        self.assertEqual("inspection", inspection["visibility"])
        self.assertEqual("canon", inspection["anchors"][0]["authority_status"])
        anchor_calls = [call for call in queries.calls if call[0] == "anchors"]
        self.assertEqual(
            [
                {"active_only": True, "include_noncanon": False},
                {"active_only": False, "include_noncanon": True},
            ],
            [call[3] for call in anchor_calls],
        )
        self.assertTrue(
            all(store.transaction_count == 0 for store in _Service.stores)
        )

    def test_cli_special_item_context_forwards_knowledge_scope(self) -> None:
        queries = _Queries()
        with tempfile.TemporaryDirectory() as temporary:
            root = _make_project(Path(temporary))
            args = cli._parser().parse_args(
                [
                    "special-item",
                    "inventory",
                    "--project-root",
                    str(root),
                    "--advantage-id",
                    "advantage-sample-core",
                    "--branch-id",
                    "branch-night",
                    "--knowledge-plane",
                    "actor_belief",
                    "--observer-id",
                    "actor-a",
                    "--ledger-limit",
                    "7",
                ]
            )
            with (
                patch("plot_state.ContinuityService", _Service),
                patch("plot_state._load_advantage_queries", return_value=queries),
            ):
                payload = cli._dispatch(args)

        self.assertEqual("样例优势核心", payload["definition"]["title"])
        self.assertEqual("generation", payload["visibility"])
        self.assertEqual(
            {
                "branch_id": "branch-night",
                "knowledge_plane": "actor_belief",
                "observer_entity_id": "actor-a",
                "ledger_limit": 7,
                "visibility": "generation",
            },
            queries.calls[-1][3],
        )

    def test_cli_knowledge_allows_explicit_inspection_visibility(self) -> None:
        queries = _Queries()
        with tempfile.TemporaryDirectory() as temporary:
            root = _make_project(Path(temporary))
            args = cli._parser().parse_args(
                [
                    "advantage",
                    "knowledge",
                    "--project-root",
                    str(root),
                    "--advantage-id",
                    "advantage-sample-core",
                    "--knowledge-plane",
                    "author_plan",
                    "--visibility",
                    "inspection",
                ]
            )
            with (
                patch("plot_state.ContinuityService", _Service),
                patch("plot_state._load_advantage_queries", return_value=queries),
            ):
                payload = cli._dispatch(args)

        self.assertEqual("ready", payload["status"])
        self.assertEqual("inspection", payload["visibility"])
        self.assertEqual(
            {
                "knowledge_plane": "author_plan",
                "observer_entity_id": None,
                "include_noncanon": False,
                "visibility": "inspection",
            },
            queries.calls[-1][3],
        )

    def test_generation_queries_redact_author_and_control_metadata(self) -> None:
        queries = _Queries()
        service = _Service(Path("PROJECT"))
        with patch(
            "plot_state._load_advantage_queries",
            return_value=queries,
        ):
            definition = cli._advantage_query_payload(
                service,
                helper_name="query_advantage_definition",
                advantage_id="advantage-sample-core",
                result_key="definition",
            )
            knowledge = cli._advantage_query_payload(
                service,
                helper_name="query_advantage_knowledge",
                advantage_id="advantage-sample-core",
                result_key="knowledge",
                kwargs={"visibility": "generation"},
            )
            inspected = cli._advantage_query_payload(
                service,
                helper_name="query_advantage_knowledge",
                advantage_id="advantage-sample-core",
                result_key="knowledge",
                kwargs={"visibility": "inspection"},
            )

        self.assertEqual("generation", definition["visibility"])
        self.assertNotIn("definition_json", definition["definition"])
        self.assertNotIn("source_event_id", definition["definition"])
        self.assertNotIn("advantage_status", definition["definition"])
        self.assertEqual(
            ["reader_disclosed"],
            [row["knowledge_plane"] for row in knowledge["knowledge"]],
        )
        self.assertIn(
            "author_plan",
            [row["knowledge_plane"] for row in inspected["knowledge"]],
        )

    def test_missing_definition_blocks_every_point_query(self) -> None:
        target_calls: list[str] = []

        def missing_definition(_connection: Any, _advantage_id: str) -> None:
            return None

        def target(name: str):
            def query(
                _connection: Any,
                _advantage_id: str,
                **_kwargs: Any,
            ) -> Any:
                target_calls.append(name)
                return (
                    []
                    if name in {"anchors", "modules", "ledger", "knowledge"}
                    else {}
                )

            return query

        queries = SimpleNamespace(
            query_advantage_definition=missing_definition,
            query_advantage_anchors=target("anchors"),
            query_advantage_runtime=target("runtime"),
            query_advantage_modules=target("modules"),
            query_advantage_ledger=target("ledger"),
            query_advantage_knowledge=target("knowledge"),
            query_advantage_progression=target("progression"),
            query_advantage_exposure=target("exposure"),
        )
        cases = (
            ("query_advantage_anchors", "anchors", False),
            ("query_advantage_runtime", "runtime", True),
            ("query_advantage_modules", "modules", False),
            ("query_advantage_ledger", "ledger", False),
            ("query_advantage_knowledge", "knowledge", False),
            ("query_advantage_progression", "progression", False),
            ("query_advantage_exposure", "exposure", False),
        )
        for payload_builder, patch_name in (
            (cli._advantage_query_payload, "plot_state._load_advantage_queries"),
            (
                mcp._advantage_query_payload,
                "plot_rag_mcp._load_advantage_queries",
            ),
        ):
            with self.subTest(surface=patch_name):
                service = _Service(Path("PROJECT"))
                with patch(patch_name, return_value=queries):
                    for helper_name, result_key, allow_none in cases:
                        with self.subTest(
                            surface=patch_name,
                            helper=helper_name,
                        ):
                            with self.assertRaisesRegex(
                                ValueError,
                                "unknown advantage",
                            ):
                                payload_builder(
                                    service,
                                    helper_name=helper_name,
                                    advantage_id="missing-advantage",
                                    result_key=result_key,
                                    allow_none=allow_none,
                                )
        self.assertEqual([], target_calls)

    def test_readonly_failure_never_retries_in_transaction(self) -> None:
        queries = _Queries()
        _Service.readonly_failure = True
        service = _Service(Path("PROJECT"))
        with patch(
            "plot_state._load_advantage_queries",
            return_value=queries,
        ):
            with self.assertRaises(sqlite3.OperationalError):
                cli._advantage_query_payload(
                    service,
                    helper_name="query_advantage_definition",
                    advantage_id="advantage-sample-core",
                    result_key="definition",
                )
        self.assertEqual(1, service.store.read_count)
        self.assertEqual(0, service.store.transaction_count)

    def test_runtime_query_preserves_a_missing_branch_as_null(self) -> None:
        queries = SimpleNamespace(
            query_advantage_definition=lambda connection, advantage_id: {
                "advantage_id": advantage_id,
                "advantage_status": "canon",
                "lifecycle_status": "active",
            },
            query_advantage_runtime=lambda connection, advantage_id, **kwargs: None
        )
        service = _Service(Path("PROJECT"))
        with patch(
            "plot_state._load_advantage_queries",
            return_value=queries,
        ):
            payload = cli._advantage_query_payload(
                service,
                helper_name="query_advantage_runtime",
                advantage_id="advantage-sample-core",
                result_key="runtime",
                kwargs={"branch_id": "unopened-branch"},
                allow_none=True,
            )
        self.assertEqual("ready", payload["status"])
        self.assertIsNone(payload["runtime"])

    def test_real_missing_queries_leave_state_database_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _make_project(Path(temporary))
            state_path = root / ".plot-rag" / "state.sqlite3"
            first_output = io.StringIO()
            with redirect_stdout(first_output):
                first_code = cli.main(
                    [
                        "advantage",
                        "definition",
                        "--project-root",
                        str(root),
                        "--advantage-id",
                        "missing-advantage",
                    ]
                )
            first = json.loads(first_output.getvalue())
            self.assertEqual(1, first_code)
            self.assertIn("unknown advantage", first["reason"])
            self.assertFalse(state_path.exists())

            second_output = io.StringIO()
            with redirect_stdout(second_output):
                second_code = cli.main(
                    [
                        "advantage",
                        "runtime",
                        "--project-root",
                        str(root),
                        "--advantage-id",
                        "missing-advantage",
                    ]
                )
            second = json.loads(second_output.getvalue())
            self.assertEqual(1, second_code)
            self.assertIn("unknown advantage", second["reason"])
            self.assertFalse(state_path.exists())

            with self.assertRaisesRegex(ValueError, "unknown advantage"):
                mcp._dispatch_tool(
                    "query_advantage_runtime",
                    {
                        "project_root": str(root),
                        "advantage_id": "missing-advantage",
                    },
                )
            self.assertFalse(state_path.exists())

    def test_existing_incomplete_database_is_not_migrated_by_query(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _make_project(Path(temporary))
            state_path = root / ".plot-rag" / "state.sqlite3"
            connection = sqlite3.connect(state_path)
            try:
                connection.execute(
                    "CREATE TABLE readonly_probe(value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO readonly_probe(value) VALUES('unchanged')"
                )
                connection.commit()
            finally:
                connection.close()
            before_bytes = state_path.read_bytes()
            before_mtime = state_path.stat().st_mtime_ns

            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(
                    [
                        "advantage",
                        "definition",
                        "--project-root",
                        str(root),
                        "--advantage-id",
                        "missing-advantage",
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(1, code)
            self.assertIn("pre-existing schema", payload["reason"])
            self.assertEqual(before_bytes, state_path.read_bytes())
            self.assertEqual(before_mtime, state_path.stat().st_mtime_ns)
            self.assertFalse(Path(f"{state_path}-wal").exists())
            self.assertFalse(Path(f"{state_path}-shm").exists())

    def test_item_observation_surfaces_default_to_generation_visibility(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _make_project(Path(temporary))
            default_args = cli._parser().parse_args(
                [
                    "item",
                    "observations",
                    "--project-root",
                    str(root),
                ]
            )
            inspection_args = cli._parser().parse_args(
                [
                    "item",
                    "observations",
                    "--project-root",
                    str(root),
                    "--visibility",
                    "inspection",
                ]
            )
            with patch(
                "plot_state.ContinuityService",
                _ItemObservationService,
            ):
                cli_default = cli._dispatch(default_args)
                cli_inspection = cli._dispatch(inspection_args)

            with patch("plot_rag_mcp._load_runtime") as runtime:
                runtime.return_value = (
                    lambda start: start,
                    None,
                    None,
                    None,
                    None,
                    _ItemObservationService,
                    SimpleNamespace(),
                )
                mcp_default = mcp._dispatch_tool(
                    "query_item_observations",
                    {"project_root": str(root)},
                )
                mcp_inspection = mcp._dispatch_tool(
                    "query_item_observations",
                    {
                        "project_root": str(root),
                        "visibility": "inspection",
                    },
                )

        for payload in (cli_default, mcp_default):
            with self.subTest(surface="generation"):
                self.assertEqual("generation", payload["visibility"])
                self.assertEqual(
                    ["public_narrative"],
                    [
                        row["knowledge_plane"]
                        for row in payload["observations"]
                    ],
                )
        for payload in (cli_inspection, mcp_inspection):
            with self.subTest(surface="inspection"):
                self.assertEqual("inspection", payload["visibility"])
                self.assertIn(
                    "author_plan",
                    [
                        row["knowledge_plane"]
                        for row in payload["observations"]
                    ],
                )
                self.assertEqual(
                    2,
                    sum(
                        row["knowledge_plane"] == "actor_belief"
                        for row in payload["observations"]
                    ),
                )
        self.assertEqual(
            [
                "generation",
                "inspection",
                "generation",
                "inspection",
            ],
            [call["visibility"] for call in _ItemObservationService.calls],
        )

    def test_mcp_catalog_marks_all_advantage_tools_read_only(self) -> None:
        tools = {
            str(tool["name"]): tool
            for tool in mcp.TOOLS
            if str(tool["name"]) in ADVANTAGE_TOOLS
        }
        self.assertEqual(ADVANTAGE_TOOLS, set(tools))
        for name, tool in tools.items():
            with self.subTest(name=name):
                self.assertTrue(tool["annotations"]["readOnlyHint"])
                schema = tool["inputSchema"]
                self.assertEqual(
                    ["project_root", "advantage_id"],
                    schema["required"],
                )
                self.assertFalse(schema["additionalProperties"])

    def test_mcp_dispatches_all_advantage_helpers(self) -> None:
        queries = _Queries()
        cases = [
            (
                "query_advantage_definition",
                {},
                "definition",
                {},
            ),
            (
                "query_advantage_anchors",
                {
                    "include_inactive": True,
                    "include_noncanon": True,
                },
                "anchors",
                {
                    "active_only": True,
                    "include_noncanon": False,
                },
            ),
            (
                "query_advantage_runtime",
                {"branch_id": "branch-a"},
                "runtime",
                {"branch_id": "branch-a"},
            ),
            (
                "query_advantage_modules",
                {"enabled_only": True},
                "modules",
                {"enabled_only": True},
            ),
            (
                "query_advantage_ledger",
                {
                    "limit": 8,
                    "entry_kind": "reward",
                    "branch_id": "branch-a",
                },
                "ledger",
                {
                    "limit": 8,
                    "entry_kind": "reward",
                    "branch_id": "branch-a",
                    "visible_module_ids": ["状态解析"],
                    "visibility": "generation",
                },
            ),
            (
                "query_advantage_knowledge",
                {
                    "knowledge_plane": "reader_disclosed",
                    "observer_entity_id": "reader",
                    "include_noncanon": True,
                    "visibility": "inspection",
                },
                "knowledge",
                {
                    "knowledge_plane": "reader_disclosed",
                    "observer_entity_id": "reader",
                    "include_noncanon": True,
                    "visibility": "inspection",
                },
            ),
            (
                "query_advantage_progression",
                {},
                "progression",
                {
                    "branch_id": "main",
                    "visible_module_ids": ["状态解析"],
                    "generation_visible_only": True,
                },
            ),
            (
                "query_advantage_exposure",
                {},
                "exposure",
                {
                    "branch_id": "main",
                    "generation_visible_only": True,
                },
            ),
            (
                "query_special_item_context",
                {
                    "branch_id": "branch-a",
                    "knowledge_plane": "actor_belief",
                    "observer_entity_id": "actor-a",
                    "ledger_limit": 6,
                },
                "context",
                {
                    "branch_id": "branch-a",
                    "knowledge_plane": "actor_belief",
                    "observer_entity_id": "actor-a",
                    "ledger_limit": 6,
                    "visibility": "generation",
                },
            ),
        ]

        with tempfile.TemporaryDirectory() as temporary:
            root = _make_project(Path(temporary))
            with (
                patch("plot_rag_mcp._load_advantage_queries", return_value=queries),
                patch("plot_rag_mcp._load_runtime") as runtime,
            ):
                runtime.return_value = (
                    lambda start: start,
                    None,
                    None,
                    None,
                    None,
                    _Service,
                    SimpleNamespace(),
                )
                for name, extra, helper_name, expected_kwargs in cases:
                    with self.subTest(name=name):
                        call_start = len(queries.calls)
                        arguments = {
                            "project_root": str(root),
                            "advantage_id": "advantage-sample-core",
                            **extra,
                        }
                        mcp._validate_tool_arguments(name, arguments)
                        payload = mcp._dispatch_tool(name, arguments)
                        self.assertEqual("ready", payload["status"])
                        self.assertEqual(
                            str(extra.get("visibility") or "generation"),
                            payload["visibility"],
                        )
                        new_calls = queries.calls[call_start:]
                        self.assertEqual("definition", new_calls[0][0])
                        call = queries.calls[-1]
                        self.assertEqual(helper_name, call[0])
                        self.assertEqual("advantage-sample-core", call[2])
                        self.assertEqual(expected_kwargs, call[3])

    def test_mcp_schema_rejects_invalid_context_filters(self) -> None:
        with self.assertRaisesRegex(ValueError, ">= 1"):
            mcp._validate_tool_arguments(
                "query_special_item_context",
                {
                    "project_root": "PROJECT",
                    "advantage_id": "advantage-sample-core",
                    "ledger_limit": 0,
                },
            )
        with self.assertRaisesRegex(ValueError, "must be one of"):
            mcp._validate_tool_arguments(
                "query_advantage_knowledge",
                {
                    "project_root": "PROJECT",
                    "advantage_id": "advantage-sample-core",
                    "knowledge_plane": "secret_unfiltered",
                },
            )
        with self.assertRaisesRegex(ValueError, "must be one of"):
            mcp._validate_tool_arguments(
                "query_advantage_knowledge",
                {
                    "project_root": "PROJECT",
                    "advantage_id": "advantage-sample-core",
                    "visibility": "everything",
                },
            )


if __name__ == "__main__":
    unittest.main()
