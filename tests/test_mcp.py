from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
SERVER = SCRIPTS / "plot_rag_mcp.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import plot_rag_mcp as mcp  # noqa: E402
import v1_runtime  # noqa: E402
from continuity import (  # noqa: E402
    ContinuityError,
    ContinuityService,
    HostApprovalAuthority,
)
from tests.test_power_spec_import import power_aggregate  # noqa: E402


COMPATIBILITY_TOOLS = {
    "prepare_plot_turn",
    "commit_plot_turn",
    "query_plot_state",
    "query_plot_craft",
    "get_plot_state",
    "doctor_plot_rag",
}
LIFECYCLE_TOOLS = {
    "propose_plot_turn",
    "list_plot_proposals",
    "inspect_plot_proposal",
    "reject_plot_proposal",
    "accept_plot_proposal",
    "retract_plot_proposal",
    "query_plot_state_at",
    "replay_plot_continuity",
}
SOURCE_MANIFEST_TOOLS = {
    "get_source_manifest_status",
    "preview_source_manifest_change",
    "propose_source_manifest_change",
}
INITIALIZATION_TOOLS = {
    "start_story_initialization",
    "dry_run_story_initialization",
    "advance_story_initialization",
    "answer_story_initialization",
    "inspect_story_initialization",
    "build_story_initialization_proposal",
    "apply_story_initialization",
    "verify_story_initialization",
    "list_story_initializations",
    "cancel_story_initialization",
}
LONGFORM_TOOLS = {
    "refresh_longform_index",
    "recover_longform_projection",
    "build_longform_context",
    "get_longform_status",
    "run_longform_benchmark",
}
POWER_TOOLS = {
    "list_power_systems",
    "query_power_state",
    "query_progression_path",
    "explain_power_action",
    "compare_power_conditions",
}
POWER_SPEC_TOOLS = {
    "validate_power_spec_change",
    "preview_power_spec_change",
    "propose_power_spec_change",
}
PERFORMANCE_TOOLS = {
    "get_plot_performance_status",
    "run_plot_performance_benchmark",
    "compare_plot_prepare_paths",
}
EXTRACTION_TOOLS = {
    "list_plot_extraction_jobs",
    "inspect_plot_extraction_job",
    "retry_plot_extraction_job",
}
EXPERIENCE_TOOLS = {
    "propose_event_experience",
    "inspect_event_experience",
    "lock_event_experience",
    "review_event_experience",
}
ITEM_TOOLS = {
    "query_item_definition",
    "query_item_instance",
    "query_item_function",
    "query_item_runtime",
    "query_item_custody",
    "query_actor_inventory",
    "query_item_history",
    "query_item_observations",
}
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
READ_ONLY_TOOLS = {
    "query_plot_craft",
    "doctor_plot_rag",
    "dry_run_story_initialization",
    "inspect_story_initialization",
    "list_story_initializations",
    "run_longform_benchmark",
    "list_power_systems",
    "query_power_state",
    "query_progression_path",
    "explain_power_action",
    "compare_power_conditions",
    "get_plot_performance_status",
    "run_plot_performance_benchmark",
    "compare_plot_prepare_paths",
    "list_plot_extraction_jobs",
    "inspect_plot_extraction_job",
    "inspect_event_experience",
    "get_source_manifest_status",
    "preview_source_manifest_change",
    "validate_power_spec_change",
    "preview_power_spec_change",
    *ITEM_TOOLS,
    *ADVANTAGE_TOOLS,
}


def make_project(base: Path) -> Path:
    root = base / "novel"
    (root / ".plot-rag").mkdir(parents=True)
    (root / "正文").mkdir()
    (root / "正文" / "第一章.md").write_text(
        "测试角色甲在测试城南站等待列车。",
        encoding="utf-8",
    )
    config = {
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
    }
    (root / ".plot-rag" / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return root


def tree_fingerprints(root: Path) -> dict[str, tuple[Any, ...]]:
    result: dict[str, tuple[Any, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            result[relative] = ("directory",)
            continue
        stat = path.stat()
        result[relative] = (
            "file",
            stat.st_size,
            stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return result


class McpServerTestCase(unittest.TestCase):
    def test_explicit_project_root_error_reports_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".plot-rag").mkdir()
            with self.assertRaisesRegex(
                ValueError,
                "explicitly provided project_root",
            ) as raised:
                mcp._project_root(str(root))
        self.assertNotIn("pass project_root explicitly", str(raised.exception))

    def test_stdio_tools_call_lists_power_systems_without_dispatch_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            message = {
                "jsonrpc": "2.0",
                "id": 41,
                "method": "tools/call",
                "params": {
                    "name": "list_power_systems",
                    "arguments": {"project_root": str(root)},
                },
            }
            completed = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(SERVER)],
                input=json.dumps(message) + "\n",
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                cwd=PLUGIN_ROOT,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            response = json.loads(completed.stdout)
            self.assertEqual(41, response["id"])
            self.assertNotIn("isError", response["result"])
            payload = response["result"]["structuredContent"]
            self.assertEqual("uninitialized", payload["status"])
            self.assertEqual([], payload["systems"])

    def test_stdio_tools_call_rejects_type_coercion_before_dispatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "novel"

            def tool_call(
                request_id: int,
                name: str,
                arguments: dict[str, Any],
            ) -> dict[str, Any]:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": name,
                        "arguments": arguments,
                    },
                }

            messages: list[dict[str, Any]] = []
            invalid_revisions = (
                ("bool", True),
                ("float", 1.0),
                ("string", "1"),
            )
            request_id = 1
            invalid_cancel_ids: list[int] = []
            start_ids: list[int] = []
            for label, revision in invalid_revisions:
                session_id = f"init-schema-{label}"
                start_ids.append(request_id)
                messages.append(
                    tool_call(
                        request_id,
                        "start_story_initialization",
                        {
                            "workspace_root": str(workspace),
                            "project_root": str(project),
                            "mode": "new",
                            "seed": "玄幻升级流",
                            "idempotency_key": f"start-{label}",
                            "session_id": session_id,
                        },
                    )
                )
                request_id += 1
                invalid_cancel_ids.append(request_id)
                messages.append(
                    tool_call(
                        request_id,
                        "cancel_story_initialization",
                        {
                            "workspace_root": str(workspace),
                            "project_root": str(project),
                            "session_id": session_id,
                            "expected_session_revision": revision,
                            "idempotency_key": f"cancel-{label}",
                        },
                    )
                )
                request_id += 1

            valid_start_id = request_id
            messages.append(
                tool_call(
                    valid_start_id,
                    "start_story_initialization",
                    {
                        "workspace_root": str(workspace),
                        "project_root": str(project),
                        "mode": "new",
                        "seed": "仙侠升级流",
                        "idempotency_key": "start-valid",
                        "session_id": "init-schema-valid",
                    },
                )
            )
            request_id += 1
            valid_cancel_id = request_id
            messages.append(
                tool_call(
                    valid_cancel_id,
                    "cancel_story_initialization",
                    {
                        "workspace_root": str(workspace),
                        "project_root": str(project),
                        "session_id": "init-schema-valid",
                        "expected_session_revision": 1,
                        "idempotency_key": "cancel-valid",
                    },
                )
            )
            request_id += 1
            invalid_boolean_id = request_id
            messages.append(
                tool_call(
                    invalid_boolean_id,
                    "list_story_initializations",
                    {
                        "workspace_root": str(workspace),
                        "project_root": str(project),
                        "active_only": "false",
                    },
                )
            )
            request_id += 1
            list_all_id = request_id
            messages.append(
                tool_call(
                    list_all_id,
                    "list_story_initializations",
                    {
                        "workspace_root": str(workspace),
                        "project_root": str(project),
                        "active_only": False,
                    },
                )
            )
            request_id += 1
            list_active_id = request_id
            messages.append(
                tool_call(
                    list_active_id,
                    "list_story_initializations",
                    {
                        "workspace_root": str(workspace),
                        "project_root": str(project),
                        "active_only": True,
                    },
                )
            )

            completed = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(SERVER)],
                input="\n".join(
                    json.dumps(message, ensure_ascii=False)
                    for message in messages
                )
                + "\n",
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                cwd=PLUGIN_ROOT,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            responses = {
                response["id"]: response
                for response in (
                    json.loads(line)
                    for line in completed.stdout.splitlines()
                    if line.strip()
                )
            }
            for start_id in [*start_ids, valid_start_id]:
                self.assertNotIn("isError", responses[start_id]["result"])
                self.assertEqual(
                    1,
                    responses[start_id]["result"]["structuredContent"][
                        "session_revision"
                    ],
                )
            for cancel_id in invalid_cancel_ids:
                result = responses[cancel_id]["result"]
                self.assertTrue(result["isError"])
                self.assertIn(
                    "expected_session_revision must be an integer",
                    result["structuredContent"]["reason"],
                )
            self.assertEqual(
                "CANCELLED",
                responses[valid_cancel_id]["result"]["structuredContent"][
                    "status"
                ],
            )
            invalid_boolean = responses[invalid_boolean_id]["result"]
            self.assertTrue(invalid_boolean["isError"])
            self.assertIn(
                "active_only must be a boolean",
                invalid_boolean["structuredContent"]["reason"],
            )
            self.assertEqual(
                4,
                responses[list_all_id]["result"]["structuredContent"]["count"],
            )
            self.assertEqual(
                3,
                responses[list_active_id]["result"]["structuredContent"][
                    "count"
                ],
            )

    def test_tool_argument_schema_validation_is_recursive_and_closed(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "missing required properties"):
            mcp._validate_tool_arguments(
                "prepare_plot_turn",
                {"project_root": "novel"},
            )
        with self.assertRaisesRegex(ValueError, "unsupported properties"):
            mcp._validate_tool_arguments(
                "list_story_initializations",
                {"workspace_root": "workspace", "unexpected": True},
            )
        with self.assertRaisesRegex(ValueError, "at least 1 characters"):
            mcp._validate_tool_arguments(
                "prepare_plot_turn",
                {"project_root": "novel", "prompt": ""},
            )
        with self.assertRaisesRegex(ValueError, "must be >= 0"):
            mcp._validate_tool_arguments(
                "cancel_story_initialization",
                {
                    "session_id": "session",
                    "expected_session_revision": -1,
                    "idempotency_key": "cancel",
                },
            )
        with self.assertRaisesRegex(ValueError, "must be <= 50"):
            mcp._validate_tool_arguments(
                "query_plot_state",
                {
                    "project_root": "novel",
                    "query": "状态",
                    "top_k": 51,
                },
            )
        with self.assertRaisesRegex(ValueError, "must be one of"):
            mcp._validate_tool_arguments(
                "prepare_plot_turn",
                {
                    "project_root": "novel",
                    "prompt": "剧情推演",
                    "artifact_stage": "unknown",
                },
            )
        with self.assertRaisesRegex(ValueError, "must be a string"):
            mcp._validate_tool_arguments(
                "list_power_systems",
                {
                    "project_root": "novel",
                    "knowledge_planes": [1],
                },
            )
        with self.assertRaisesRegex(ValueError, "duplicates an earlier"):
            mcp._validate_tool_arguments(
                "list_power_systems",
                {
                    "project_root": "novel",
                    "knowledge_planes": ["objective", "objective"],
                },
            )
        with self.assertRaisesRegex(ValueError, "at least 1 properties"):
            mcp._validate_tool_arguments(
                "answer_story_initialization",
                {
                    "session_id": "session",
                    "answers": {},
                    "expected_session_revision": 1,
                    "idempotency_key": "answer",
                },
            )
        with self.assertRaisesRegex(ValueError, "at least 1 properties"):
            mcp._validate_tool_arguments(
                "preview_source_manifest_change",
                {
                    "project_root": "novel",
                    "plan": {},
                    "expected_canon_revision": 0,
                },
            )
        with self.assertRaisesRegex(ValueError, "at least 1 properties"):
            mcp._validate_tool_arguments(
                "validate_power_spec_change",
                {"power_spec": {}},
            )
        mcp._validate_tool_arguments(
            "list_power_systems",
            {
                "project_root": "novel",
                "knowledge_planes": ["objective"],
                "include_provisional": False,
            },
        )

    def test_stdio_handshake_catalog_annotations_and_no_grant_issuer(self) -> None:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        completed = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(SERVER)],
            input="\n".join(json.dumps(message) for message in messages) + "\n",
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            cwd=PLUGIN_ROOT,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        responses = [
            json.loads(line)
            for line in completed.stdout.splitlines()
            if line.strip()
        ]
        self.assertEqual(
            "2025-06-18",
            responses[0]["result"]["protocolVersion"],
        )
        self.assertEqual(
            "1.6.4",
            responses[0]["result"]["serverInfo"]["version"],
        )
        catalog = {
            item["name"]: item
            for item in responses[1]["result"]["tools"]
        }
        self.assertEqual(
            COMPATIBILITY_TOOLS
            | LIFECYCLE_TOOLS
            | SOURCE_MANIFEST_TOOLS
             | INITIALIZATION_TOOLS
             | LONGFORM_TOOLS
             | POWER_TOOLS
             | POWER_SPEC_TOOLS
             | PERFORMANCE_TOOLS
            | EXTRACTION_TOOLS
            | EXPERIENCE_TOOLS
            | ITEM_TOOLS
            | ADVANTAGE_TOOLS,
            set(catalog),
        )
        for name, tool in catalog.items():
            self.assertEqual(
                name in READ_ONLY_TOOLS,
                bool(
                    (tool.get("annotations") or {}).get("readOnlyHint")
                ),
                name,
            )
        self.assertNotIn(
            "system_id",
            catalog["list_power_systems"]["inputSchema"]["properties"],
        )
        self.assertIn(
            "system_id",
            catalog["query_power_state"]["inputSchema"]["properties"],
        )
        for name in (
            ITEM_TOOLS
            | PERFORMANCE_TOOLS
            | {
                "get_source_manifest_status",
                "preview_source_manifest_change",
            }
        ):
            self.assertTrue(
                (catalog[name].get("annotations") or {}).get(
                    "readOnlyHint"
                ),
                name,
            )
        for name in {
            "retry_plot_extraction_job",
            "propose_event_experience",
            "lock_event_experience",
            "review_event_experience",
            "propose_source_manifest_change",
            "propose_power_spec_change",
        }:
            self.assertFalse(
                (catalog[name].get("annotations") or {}).get(
                    "readOnlyHint",
                    False,
                ),
                name,
            )
        issuer_like = {
            name
            for name in catalog
            if "grant" in name.casefold()
            or "approval" in name.casefold()
            or name.casefold().startswith("issue_")
        }
        self.assertEqual(set(), issuer_like)
        for name in {
            "accept_plot_proposal",
            "retract_plot_proposal",
        }:
            required = set(catalog[name]["inputSchema"]["required"])
            self.assertIn("approval_id", required)
        self.assertEqual(
            {"idempotency_key"},
            set(
                catalog["start_story_initialization"]["inputSchema"][
                    "required"
                ]
            ),
        )
        self.assertNotIn(
            "required",
            catalog["dry_run_story_initialization"]["inputSchema"],
        )
        self.assertEqual(
            {
                "proposal_id",
                "expected_canon_revision",
                "idempotency_key",
            },
            set(
                catalog["apply_story_initialization"]["inputSchema"][
                    "required"
                ]
            ),
        )
        self.assertEqual(
            {
                "project_root",
                "plan",
                "expected_canon_revision",
            },
            set(
                catalog["preview_source_manifest_change"]["inputSchema"][
                    "required"
                ]
            ),
        )
        self.assertEqual(
            {"power_spec"},
            set(
                catalog["validate_power_spec_change"]["inputSchema"][
                    "required"
                ]
            ),
        )
        self.assertEqual(
            {
                "project_root",
                "power_spec",
                "expected_canon_revision",
            },
            set(
                catalog["preview_power_spec_change"]["inputSchema"][
                    "required"
                ]
            ),
        )
        self.assertEqual(
            {
                "project_root",
                "power_spec",
                "expected_canon_revision",
                "idempotency_key",
            },
            set(
                catalog["propose_power_spec_change"]["inputSchema"][
                    "required"
                ]
            ),
        )
        self.assertEqual(
            {
                "project_root",
                "plan",
                "expected_canon_revision",
                "idempotency_key",
            },
            set(
                catalog["propose_source_manifest_change"]["inputSchema"][
                    "required"
                ]
            ),
        )

    def test_source_manifest_tools_preview_and_propose_without_grant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            service = ContinuityService(root)
            self.assertEqual(
                {"head": 0, "active": 0},
                service.get_canon_revisions(),
            )
            source_path = root / "正文" / "第一章.md"
            source = {
                "source_path": "正文/第一章.md",
                "content_hash": hashlib.sha256(
                    source_path.read_bytes()
                ).hexdigest(),
                "source_role": "canon",
                "metadata": {
                    "artifact_stage": "published",
                    "indexable": True,
                },
            }
            plan = {
                "schema_version": (
                    "plot-rag-source-manifest-migration-plan/v1"
                ),
                "generated_at": "2026-07-20T00:00:00+00:00",
                "project_root": str(root.resolve()),
                "expected_canon_revision": 0,
                "head_canon_revision": 0,
                "retire_commits": [],
                "baseline": {},
                "operations": {
                    "deactivate_entry_ids": [],
                    "retain_entry_ids": [],
                    "upserts": [source],
                },
                "target": {
                    "active_rows": 1,
                    "unique_paths": 1,
                    "sources": [source],
                },
            }

            status = mcp._dispatch_tool(
                "get_source_manifest_status",
                {"project_root": str(root)},
            )
            preview = mcp._dispatch_tool(
                "preview_source_manifest_change",
                {
                    "project_root": str(root),
                    "plan": plan,
                    "expected_canon_revision": 0,
                },
            )
            proposed = mcp._dispatch_tool(
                "propose_source_manifest_change",
                {
                    "project_root": str(root),
                    "plan": plan,
                    "expected_canon_revision": 0,
                    "idempotency_key": "mcp-source-manifest-propose",
                },
            )

            self.assertEqual("ready", status["status"])
            self.assertTrue(status["read_only"])
            self.assertEqual("ready", preview["status"])
            self.assertTrue(preview["read_only"])
            self.assertEqual("proposed", proposed["status"])
            self.assertEqual(
                "source_manifest_change",
                proposed["proposal"]["proposal_kind"],
            )
            self.assertEqual(
                {"head": 0, "active": 0},
                service.get_canon_revisions(),
            )
            with service.store.read_connection() as connection:
                grant_count = connection.execute(
                    "SELECT COUNT(*) FROM approval_grants"
                ).fetchone()[0]
            self.assertEqual(0, grant_count)

    def test_power_spec_tools_validate_preview_and_propose_without_grant(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            service = ContinuityService(root)
            service.store.ensure_schema()
            power_spec = power_aggregate()

            validated = mcp._dispatch_tool(
                "validate_power_spec_change",
                {"power_spec": power_spec},
            )
            preview = mcp._dispatch_tool(
                "preview_power_spec_change",
                {
                    "project_root": str(root),
                    "power_spec": power_spec,
                    "expected_canon_revision": 0,
                },
            )
            proposed = mcp._dispatch_tool(
                "propose_power_spec_change",
                {
                    "project_root": str(root),
                    "power_spec": power_spec,
                    "expected_canon_revision": 0,
                    "idempotency_key": "mcp-power-spec-propose",
                },
            )

            self.assertEqual("ready", validated["status"])
            self.assertTrue(validated["read_only"])
            self.assertEqual("ready", preview["status"])
            self.assertTrue(preview["read_only"])
            self.assertEqual("proposed", proposed["status"])
            self.assertEqual(
                "power_spec_change",
                proposed["proposal"]["proposal_kind"],
            )
            self.assertEqual(
                {"head": 0, "active": 0},
                service.get_canon_revisions(),
            )
            with service.store.read_connection() as connection:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM approval_grants"
                    ).fetchone()[0],
                )

    def test_source_manifest_read_only_tools_do_not_create_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            before = tree_fingerprints(root)
            plan = {
                "schema_version": (
                    "plot-rag-source-manifest-migration-plan/v1"
                ),
                "project_root": str(root.resolve()),
                "expected_canon_revision": 0,
                "retire_commits": [],
                "operations": {
                    "deactivate_entry_ids": [],
                    "retain_entry_ids": [],
                    "upserts": [],
                },
                "target": {
                    "active_rows": 0,
                    "unique_paths": 0,
                    "sources": [],
                },
            }

            for name, arguments in (
                (
                    "get_source_manifest_status",
                    {"project_root": str(root)},
                ),
                (
                    "preview_source_manifest_change",
                    {
                        "project_root": str(root),
                        "plan": plan,
                        "expected_canon_revision": 0,
                    },
                ),
            ):
                with self.subTest(tool=name):
                    with self.assertRaises(ContinuityError) as caught:
                        mcp._dispatch_tool(name, arguments)
                    self.assertEqual(
                        "SOURCE_MANIFEST_STATE_NOT_CREATED",
                        caught.exception.code,
                    )
                    self.assertEqual(before, tree_fingerprints(root))

    def test_initialization_start_and_dry_run_need_no_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "brand-new-novel"
            dry = mcp._dispatch_tool(
                "dry_run_story_initialization",
                {
                    "workspace_root": str(workspace),
                    "project_root": str(project),
                    "mode": "new",
                    "seed": "玄幻升级流",
                },
            )
            self.assertFalse(dry["persisted"])
            self.assertFalse(dry["database_touched"])
            self.assertFalse(
                (workspace / ".plot-rag-init" / "init.sqlite3").exists()
            )
            self.assertFalse((project / ".plot-rag" / "config.json").exists())

            started = mcp._dispatch_tool(
                "start_story_initialization",
                {
                    "workspace_root": str(workspace),
                    "project_root": str(project),
                    "mode": "new",
                    "seed": "玄幻升级流",
                    "idempotency_key": "mcp-start",
                },
            )
            self.assertTrue(started["persisted"])
            self.assertTrue(started["session_id"].startswith("init-"))
            self.assertFalse((project / ".plot-rag" / "config.json").exists())

            listed = mcp._dispatch_tool(
                "list_story_initializations",
                {
                    "workspace_root": str(workspace),
                    "project_root": str(project),
                },
            )
            self.assertEqual(1, listed["count"])
            inspected = mcp._dispatch_tool(
                "inspect_story_initialization",
                {
                    "workspace_root": str(workspace),
                    "project_root": str(project),
                    "session_id": started["session_id"],
                    "view": "questions",
                },
            )
            self.assertTrue(inspected["read_only"])
            self.assertEqual(started["session_id"], inspected["session_id"])

            cancelled = mcp._dispatch_tool(
                "cancel_story_initialization",
                {
                    "workspace_root": str(workspace),
                    "project_root": str(project),
                    "session_id": started["session_id"],
                    "expected_session_revision": started[
                        "session_revision"
                    ],
                    "idempotency_key": "mcp-cancel",
                    "reason": "test complete",
                },
            )
            self.assertEqual("CANCELLED", cancelled["status"])

    def test_v3_doctor_dispatches_unified_zero_write_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            before = tree_fingerprints(root)

            result = mcp._dispatch_tool(
                "doctor_plot_rag",
                {"project_root": str(root)},
            )

            after = tree_fingerprints(root)
            self.assertEqual(before, after)
            self.assertTrue(result["zero_write"])
            self.assertTrue(result["read_only_snapshot"])
            self.assertEqual(3, result["config_version"])
            self.assertFalse(result["bootstrap_ready"])
            expected = {
                "config",
                "state",
                "continuity",
                "source_manifest",
                "authority_index",
                "initialization_store",
                "longform_memory",
                "longform_summary",
                "longform_method",
                "longform_projection",
                "bootstrap_readiness",
                "craft_catalog",
                "snapshot",
                "remote",
            }
            self.assertEqual(expected, set(result["components"]))
            self.assertEqual(
                "not_created",
                result["components"]["continuity"]["status"],
            )
            self.assertEqual(
                "not_created",
                result["components"]["bootstrap_readiness"]["status"],
            )
            method = result["components"]["longform_method"]
            self.assertEqual("ok", method["status"])
            self.assertGreaterEqual(
                method["method_pack"]["cards_count"],
                9,
            )
            self.assertFalse(
                any(
                    path.suffix in {".sqlite", ".sqlite3"}
                    or path.name.endswith(("-wal", "-shm"))
                    for path in root.rglob("*")
                )
            )

    def test_v2_doctor_keeps_legacy_response_and_zero_write_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            config_path = root / ".plot-rag" / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["version"] = 2
            config.pop("config_version", None)
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            before = tree_fingerprints(root)

            result = mcp._dispatch_tool(
                "doctor_plot_rag",
                {"project_root": str(root)},
            )

            self.assertEqual(before, tree_fingerprints(root))
            self.assertNotIn("components", result)
            self.assertNotIn("runtime_version", result)
            config_check = next(
                item
                for item in result["checks"]
                if item["name"] == "config"
            )
            self.assertEqual(2, config_check["version"])

    def test_lifecycle_tools_consume_host_grants_and_query_at_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            service = ContinuityService(root)
            actor = service.register_entity("character", "测试角色甲")["entity_id"]
            proposal = service.save_proposal(
                events=[
                    {
                        "event_type": "state",
                        "entity_id": actor,
                        "field": "condition",
                        "value": "警惕",
                        "scope": "current",
                    }
                ],
                artifact_id="chapter-1",
                artifact_stage="final",
                chapter_no=1,
                prepared_canon_revision=0,
            )

            listed = mcp._dispatch_tool(
                "list_plot_proposals",
                {"project_root": str(root), "canon_status": "proposed"},
            )
            self.assertEqual(1, listed["count"])
            inspected = mcp._dispatch_tool(
                "inspect_plot_proposal",
                {
                    "project_root": str(root),
                    "proposal_id": proposal["proposal_id"],
                },
            )
            self.assertEqual(
                proposal["proposal_id"],
                inspected["proposal"]["proposal_id"],
            )
            for invalid_revision in (False, 0.0, 0.5, "0"):
                with self.subTest(
                    field="expected_canon_revision",
                    value=invalid_revision,
                ):
                    with self.assertRaises(ContinuityError) as invalid:
                        mcp._dispatch_tool(
                            "accept_plot_proposal",
                            {
                                "project_root": str(root),
                                "proposal_id": proposal["proposal_id"],
                                "approval_id": "not-a-grant",
                                "expected_canon_revision": invalid_revision,
                            },
                        )
                    self.assertEqual(
                        "INVALID_FIELD",
                        invalid.exception.code,
                    )
            with self.assertRaises(ContinuityError) as missing:
                mcp._dispatch_tool(
                    "accept_plot_proposal",
                    {
                        "project_root": str(root),
                        "proposal_id": proposal["proposal_id"],
                        "approval_id": "not-a-grant",
                        "expected_canon_revision": 0,
                    },
                )
            self.assertEqual(
                "APPROVAL_GRANT_NOT_FOUND",
                missing.exception.code,
            )

            host = HostApprovalAuthority(
                service,
                issuer="mcp-unittest-host",
                channel="interactive_test",
            )
            grant = host.issue(
                proposal["proposal_id"],
                expected_canon_revision=0,
                operations=("accept",),
            )
            with patch.object(
                v1_runtime,
                "_project_after_commit",
                return_value={"status": "completed"},
            ):
                accepted = mcp._dispatch_tool(
                    "accept_plot_proposal",
                    {
                        "project_root": str(root),
                        "proposal_id": proposal["proposal_id"],
                        "approval_id": grant["approval_id"],
                        "expected_canon_revision": 0,
                    },
                )
            self.assertEqual("accepted", accepted["status"])
            compatibility = mcp._dispatch_tool(
                "query_plot_state",
                {
                    "project_root": str(root),
                    "query": "测试角色甲现在是什么状态",
                    "categories": ["character_state"],
                    "top_k": 10,
                },
            )
            self.assertEqual("strict_proposal", compatibility["lifecycle_mode"])
            self.assertEqual(
                ["警惕"],
                [item["value"] for item in compatibility["facts"]],
            )
            at_chapter = mcp._dispatch_tool(
                "query_plot_state_at",
                {
                    "project_root": str(root),
                    "entity_id": actor,
                    "chapter_no": 1,
                    "scene_index": 0,
                },
            )
            self.assertEqual(
                ["警惕"],
                [item["value"] for item in at_chapter["facts"]],
            )
            for field, value in (
                ("chapter_no", 1.0),
                ("scene_index", 0.0),
            ):
                with self.subTest(field=field, value=value):
                    arguments = {
                        "project_root": str(root),
                        "entity_id": actor,
                        "chapter_no": 1,
                        "scene_index": 0,
                    }
                    arguments[field] = value
                    with self.assertRaises(ContinuityError) as invalid:
                        mcp._dispatch_tool(
                            "query_plot_state_at",
                            arguments,
                        )
                    self.assertEqual(
                        "INVALID_FIELD",
                        invalid.exception.code,
                    )
            replayed = mcp._dispatch_tool(
                "replay_plot_continuity",
                {"project_root": str(root)},
            )
            self.assertEqual("completed", replayed["status"])

    def test_reject_and_longform_benchmark_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            service = ContinuityService(root)
            actor = service.register_entity("character", "林川")["entity_id"]
            proposal = service.save_proposal(
                events=[
                    {
                        "event_type": "state",
                        "entity_id": actor,
                        "field": "condition",
                        "value": "待定",
                        "scope": "planned",
                    }
                ],
                artifact_id="outline-2",
                artifact_stage="outline",
                chapter_no=2,
                prepared_canon_revision=0,
            )
            rejected = mcp._dispatch_tool(
                "reject_plot_proposal",
                {
                    "project_root": str(root),
                    "proposal_id": proposal["proposal_id"],
                    "reason": "discarded branch",
                    "idempotency_key": "mcp-reject",
                },
            )
            self.assertEqual("rejected", rejected["status"])
            self.assertEqual(
                "rejected",
                rejected["proposal"]["canon_status"],
            )

        benchmark = mcp._dispatch_tool(
            "run_longform_benchmark",
            {},
        )
        self.assertEqual("passed", benchmark["status"])
        self.assertEqual(0, benchmark["result"]["fp"])
        self.assertEqual(0, benchmark["result"]["fn"])

    def test_v15_performance_extraction_experience_and_item_dispatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            queue = MagicMock()
            queue.list_jobs.return_value = [{"job_id": "job-1"}]
            queue.retry.return_value = {
                "job_id": "job-1",
                "status": "queued",
            }
            experience = MagicMock()
            experience.get_control_revision.return_value = 3
            experience.get_contract.return_value = {
                "contract_id": "contract-1"
            }
            expected_item = {
                "status": "ready",
                "definition": {"item_definition_id": "item-def-1"},
            }
            with (
                patch(
                    "performance_runtime.get_status",
                    return_value={"status": "ready"},
                ) as performance_status,
                patch(
                    "extraction_jobs.ExtractionJobQueue",
                    return_value=queue,
                ),
                patch(
                    "event_experience.EventExperienceService.for_project",
                    return_value=experience,
                ),
                patch.object(
                    ContinuityService,
                    "query_item_definition",
                    return_value=expected_item,
                    create=True,
                ) as item_definition,
            ):
                status = mcp._dispatch_tool(
                    "get_plot_performance_status",
                    {"project_root": str(root)},
                )
                listed = mcp._dispatch_tool(
                    "list_plot_extraction_jobs",
                    {
                        "project_root": str(root),
                        "statuses": ["failed"],
                        "limit": 10,
                    },
                )
                retried = mcp._dispatch_tool(
                    "retry_plot_extraction_job",
                    {
                        "project_root": str(root),
                        "job_id": "job-1",
                        "expected_attempt_count": 2,
                    },
                )
                inspected = mcp._dispatch_tool(
                    "inspect_event_experience",
                    {
                        "project_root": str(root),
                        "contract_id": "contract-1",
                    },
                )
                definition = mcp._dispatch_tool(
                    "query_item_definition",
                    {
                        "project_root": str(root),
                        "item_definition_id": "item-def-1",
                    },
                )

            self.assertEqual("ready", status["status"])
            self.assertEqual(1, listed["count"])
            self.assertEqual("queued", retried["status"])
            self.assertEqual(3, inspected["control_revision"])
            self.assertEqual(expected_item, definition)
            performance_status.assert_called_once_with(root.resolve())
            queue.list_jobs.assert_called_once_with(
                status=["failed"],
                branch_id=None,
                sequence_no=None,
                receipt_id=None,
                limit=10,
                offset=0,
            )
            queue.retry.assert_called_once_with(
                "job-1",
                expected_attempt_count=2,
                next_attempt_at=None,
            )
            experience.get_contract.assert_called_once_with("contract-1")
            item_definition.assert_called_once_with("item-def-1")

    def test_compare_plot_prepare_paths_accepts_embedded_reports(self) -> None:
        left = {
            "telemetry": {
                "prepare": {
                    "new_query": {"p50_ms": 10.0},
                }
            }
        }
        right = {
            "telemetry": {
                "prepare": {
                    "new_query": {"p50_ms": 8.0},
                }
            }
        }
        result = mcp._dispatch_tool(
            "compare_plot_prepare_paths",
            {
                "left_report": left,
                "right_report": right,
            },
        )
        self.assertEqual("compared", result["status"])
        self.assertIn(
            "prepare.new_query.p50_ms",
            result["improvements"],
        )

    def test_item_observations_dispatch_uses_real_v6_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            ContinuityService(root)
            result = mcp._dispatch_tool(
                "query_item_observations",
                {
                    "project_root": str(root),
                    "knowledge_plane": "objective",
                },
            )
        self.assertEqual([], result["observations"])
        self.assertEqual(1, result["item_projection_schema_version"])
        self.assertRegex(
            result["item_projection_hash"],
            r"^item_projection_[0-9a-f]{64}$",
        )

    def test_initialization_apply_dispatch_does_not_require_preexisting_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "new-target"
            expected = {"status": "completed", "bootstrap_ready": True}
            with patch.object(
                v1_runtime,
                "apply_initialization_proposal",
                return_value=expected,
            ) as apply:
                result = mcp._dispatch_tool(
                    "apply_story_initialization",
                    {
                        "workspace_root": str(workspace),
                        "project_root": str(project),
                        "proposal_id": "init-proposal-test",
                        "approval_id": "host-issued-token",
                        "expected_canon_revision": 0,
                        "idempotency_key": "mcp-init-apply",
                    },
                )
            self.assertEqual(expected, result)
            apply.assert_called_once_with(
                project.resolve(),
                "init-proposal-test",
                approval_id="host-issued-token",
                expected_canon_revision=0,
                idempotency_key="mcp-init-apply",
                workspace_root=workspace.resolve(),
                materialize=True,
            )

    def test_initialization_apply_without_approval_returns_power_requirement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = workspace / "new-target"
            expected = {
                "status": "POWER_SPEC_APPROVAL_REQUIRED",
                "power_spec_proposal_id": "proposal-power",
                "expected_canon_revision": 0,
            }
            with (
                patch.object(
                    v1_runtime,
                    "prepare_initialization_apply",
                    return_value=expected,
                ) as prepare,
                patch.object(
                    v1_runtime,
                    "apply_initialization_proposal",
                ) as apply,
            ):
                result = mcp._dispatch_tool(
                    "apply_story_initialization",
                    {
                        "workspace_root": str(workspace),
                        "project_root": str(project),
                        "proposal_id": "init-proposal-test",
                        "expected_canon_revision": 0,
                        "idempotency_key": "mcp-init-first-stage",
                    },
                )
            self.assertEqual(expected, result)
            prepare.assert_called_once_with(
                project.resolve(),
                "init-proposal-test",
                workspace_root=workspace.resolve(),
            )
            apply.assert_not_called()

    def test_generic_accept_passes_workspace_for_initialization_rebase(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            project = make_project(workspace)
            expected = {
                "status": "accepted",
                "initialization_rebase": {"status": "registered"},
            }
            with patch.object(
                v1_runtime,
                "accept_plot_proposal",
                return_value=expected,
            ) as accept:
                result = mcp._dispatch_tool(
                    "accept_plot_proposal",
                    {
                        "workspace_root": str(workspace),
                        "project_root": str(project),
                        "proposal_id": "proposal-power",
                        "approval_id": "host-issued-token",
                        "expected_canon_revision": 0,
                    },
                )
            self.assertEqual(expected, result)
            accept.assert_called_once_with(
                project.resolve(),
                "proposal-power",
                approval_id="host-issued-token",
                expected_canon_revision=0,
                workspace_root=workspace.resolve(),
            )


if __name__ == "__main__":
    unittest.main()
