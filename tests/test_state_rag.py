from __future__ import annotations

import hashlib
import json
import email.utils
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
HOOKS = PLUGIN_ROOT / "hooks"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import state_rag as state_runtime
from state_rag import commit_turn, doctor, dump_state, prepare_turn, query_craft


class MockModelHandler(BaseHTTPRequestHandler):
    deltas: list[dict[str, Any]] = []
    chat_calls = 0
    embedding_calls = 0
    rerank_calls = 0

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        return value if isinstance(value, dict) else {}

    def _send(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [
            float(1 + text.count("测试角色甲")),
            float(1 + text.count("测试城")),
            float(1 + text.count("道具") + text.count("钥匙") + text.count("通行证")),
            float(1 + sum(ord(char) for char in text) % 17),
        ]

    def do_POST(self) -> None:
        payload = self._payload()
        if self.path.endswith("/embeddings"):
            type(self).embedding_calls += 1
            inputs = payload.get("input") or []
            if isinstance(inputs, str):
                inputs = [inputs]
            self._send(
                {
                    "data": [
                        {"index": index, "embedding": self._vector(str(value))}
                        for index, value in enumerate(inputs)
                    ]
                }
            )
            return
        if self.path.endswith("/rerank"):
            type(self).rerank_calls += 1
            documents = payload.get("documents") or []
            query = str(payload.get("query") or "")
            ranked = []
            for index, document in enumerate(documents):
                text = str(document)
                overlap = sum(token in text for token in ("测试角色甲", "位置", "道具", "测试城"))
                ranked.append({"index": index, "relevance_score": float(overlap) + 1 / (index + 1)})
            ranked.sort(key=lambda item: item["relevance_score"], reverse=True)
            top_n = int(payload.get("top_n") or len(ranked))
            self._send({"results": ranked[:top_n]})
            return
        if self.path.endswith("/chat/completions"):
            type(self).chat_calls += 1
            self._send(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"deltas": type(self).deltas}, ensure_ascii=False
                                )
                            }
                        }
                    ]
                }
            )
            return
        self.send_error(404)


class RedirectingRemoteHandler(BaseHTTPRequestHandler):
    location = ""
    authorization_headers: list[str] = []

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        type(self).authorization_headers.append(
            str(self.headers.get("Authorization") or "")
        )
        self.send_response(302)
        self.send_header("Location", type(self).location)
        self.send_header("Content-Length", "0")
        self.end_headers()


class RedirectCredentialSinkHandler(BaseHTTPRequestHandler):
    calls = 0
    authorization_headers: list[str] = []

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def _capture(self) -> None:
        type(self).calls += 1
        type(self).authorization_headers.append(
            str(self.headers.get("Authorization") or "")
        )
        encoded = b'{"choices":[{"message":{"content":"{}"}}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        self._capture()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self._capture()


class KeepAliveRemoteHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    client_ports: list[int] = []
    user_agents: list[str] = []
    authorization_headers: list[str] = []

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        type(self).client_ports.append(int(self.client_address[1]))
        type(self).user_agents.append(str(self.headers.get("User-Agent") or ""))
        type(self).authorization_headers.append(
            str(self.headers.get("Authorization") or "")
        )
        encoded = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class BoundedConcurrencyRemoteHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    lock = threading.Lock()
    release_requests = threading.Event()
    concurrency_reached = threading.Event()
    calls = 0
    active = 0
    max_active = 0

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        with type(self).lock:
            type(self).calls += 1
            type(self).active += 1
            type(self).max_active = max(
                type(self).max_active,
                type(self).active,
            )
            if (
                type(self).active
                >= state_runtime._REMOTE_MAX_CONNECTIONS_PER_SERVICE
            ):
                type(self).concurrency_reached.set()
        try:
            type(self).release_requests.wait(timeout=5)
            encoded = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        finally:
            with type(self).lock:
                type(self).active -= 1


class RetrySequenceRemoteHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    lock = threading.Lock()
    statuses: list[int] = []
    retry_after: list[str | None] = []
    calls = 0

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        with type(self).lock:
            index = type(self).calls
            type(self).calls += 1
            status = (
                type(self).statuses[index]
                if index < len(type(self).statuses)
                else type(self).statuses[-1]
            )
            retry_after = (
                type(self).retry_after[index]
                if index < len(type(self).retry_after)
                else None
            )
        encoded = (
            b'{"ok":true}'
            if status < 300
            else b'{"error":"temporarily throttled"}'
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if retry_after is not None:
            self.send_header("Retry-After", retry_after)
        self.end_headers()
        self.wfile.write(encoded)


class CoordinatedRetryRemoteHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    lock = threading.Lock()
    first_wave = threading.Barrier(4)
    counts: dict[str, int] = {}
    calls = 0
    active_retry = 0
    max_retry_active = 0

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        request_id = str(payload.get("request_id") or "")
        with type(self).lock:
            count = type(self).counts.get(request_id, 0) + 1
            type(self).counts[request_id] = count
            type(self).calls += 1
        if count == 1:
            try:
                type(self).first_wave.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            # Keep the first wave together long enough for every caller to
            # observe the same throttled response before its retry.
            time.sleep(0.03)
            status = 429
            encoded = b'{"error":"temporarily throttled"}'
        else:
            with type(self).lock:
                type(self).active_retry += 1
                type(self).max_retry_active = max(
                    type(self).max_retry_active,
                    type(self).active_retry,
                )
            try:
                time.sleep(0.03)
                status = 200
                encoded = b'{"ok":true}'
            finally:
                with type(self).lock:
                    type(self).active_retry -= 1
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if status == 429:
            self.send_header("Retry-After", "0")
        self.end_headers()
        self.wfile.write(encoded)


class RemoteConnectionPoolLifecycleTests(unittest.TestCase):
    def test_exit_cleanup_is_registered_and_close_all_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "closed.txt"
            code = f"""
import sys
from pathlib import Path

sys.path.insert(0, {str(SCRIPTS)!r})
import state_rag

marker = Path({str(marker)!r})

class FakeConnection:
    sock = object()

    def __init__(self, label):
        self.label = label

    def close(self):
        with marker.open("a", encoding="utf-8") as handle:
            handle.write(self.label + "\\n")

pool = state_rag._REMOTE_CONNECTION_POOL
key = ("test", "https", "example.invalid", 443)
with pool._lock:
    pool._idle[key] = [FakeConnection("manual")]
pool.close_all()
pool.close_all()
with pool._lock:
    pool._idle[key] = [FakeConnection("atexit")]
"""
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    "-W",
                    "error::ResourceWarning",
                    "-c",
                    code,
                ],
                cwd=PLUGIN_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                0,
                completed.returncode,
                msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
            )
            self.assertEqual(
                ["manual", "atexit"],
                marker.read_text(encoding="utf-8").splitlines(),
            )


class StateRagTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_trusted_hosts = os.environ.get("PLOT_RAG_TRUSTED_HOSTS")
        os.environ["PLOT_RAG_TRUSTED_HOSTS"] = "127.0.0.1"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockModelHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        if cls.original_trusted_hosts is None:
            os.environ.pop("PLOT_RAG_TRUSTED_HOSTS", None)
        else:
            os.environ["PLOT_RAG_TRUSTED_HOSTS"] = cls.original_trusted_hosts

    def setUp(self) -> None:
        MockModelHandler.deltas = []
        MockModelHandler.chat_calls = 0
        MockModelHandler.embedding_calls = 0
        MockModelHandler.rerank_calls = 0

    def make_project(self, base: Path) -> Path:
        root = base / "novel"
        (root / ".plot-rag").mkdir(parents=True)
        (root / "settings").mkdir()
        (root / "settings" / "facts.md").write_text(
            "# 当前依据\n\n继续推进剧情，安排测试角色甲抵达测试城后的行动。"
            "测试角色甲从测试城南站开始本轮行动。\n",
            encoding="utf-8",
        )
        config = {
            "version": 2,
            "enabled": True,
            "grill": {
                "enabled": False,
            },
            "authority_globs": ["settings/*.md"],
            "state": {
                "enabled": True,
                "db_path": ".plot-rag/state.sqlite3",
                "snapshot_path": ".plot-rag/state_snapshot.json",
                "commit_dir": ".plot-rag/commits",
                "auto_retrieve": True,
                "auto_record": True,
                "fail_closed": False,
                "categories": [
                    "character_state",
                    "relationship",
                    "location",
                    "inventory",
                    "story_time",
                    "world_state",
                ],
                "top_k": 12,
                "max_context_chars": 12000,
                "min_confidence": 0.72,
            },
            "remote": {
                "timeout_seconds": 5,
                "embedding": {
                    "enabled": True,
                    "base_url": self.base_url,
                    "model": "mock-embedding",
                    "api_key_env": "PLOT_RAG_EMBED_API_KEY",
                    "api_key_required": False,
                },
                "rerank": {
                    "enabled": True,
                    "base_url": self.base_url,
                    "model": "mock-rerank",
                    "api_key_env": "PLOT_RAG_RERANK_API_KEY",
                    "api_key_required": False,
                },
                "extract": {
                    "enabled": True,
                    "base_url": self.base_url,
                    "model": "mock-chat",
                    "api_key_env": "PLOT_RAG_LLM_API_KEY",
                    "api_key_required": False,
                },
            },
        }
        (root / ".plot-rag" / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return root

    @staticmethod
    def storage_fingerprint(path: Path) -> tuple[bool, int, int, str]:
        if not path.is_file():
            return False, 0, 0, ""
        stat = path.stat()
        return (
            True,
            int(stat.st_size),
            int(stat.st_mtime_ns),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def state_storage_fingerprints(
        self, root: Path
    ) -> dict[str, tuple[bool, int, int, str]]:
        database = root / ".plot-rag" / "state.sqlite3"
        return {
            "database": self.storage_fingerprint(database),
            "wal": self.storage_fingerprint(Path(str(database) + "-wal")),
            "shm": self.storage_fingerprint(Path(str(database) + "-shm")),
        }

    def tree_fingerprints(
        self, root: Path
    ) -> dict[str, tuple[bool, int, int, str]]:
        return {
            path.relative_to(root).as_posix(): self.storage_fingerprint(path)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def run_hook(self, root: Path, payload: dict[str, Any], *, stop: bool = False) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-B", "-X", "utf8", str(HOOKS / "plot_progression_gate.py")]
        if stop:
            command.append("--stop")
        return subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            cwd=root,
        )

    def test_hook_prepare_commit_and_next_turn_retrieval(self) -> None:
        assistant_text = "\n".join(
            [
                "测试角色甲已经抵达测试城，落脚在南站候车厅。",
                "青铜钥匙仍由测试角色甲持有。",
                "列车通行证也由测试角色甲持有。",
                "测试角色丁与测试角色甲正式结成盟友。",
                "测试角色戊与测试角色甲形成债务关系。",
                "此刻是景历十二年三月初七夜。",
                "下一章测试角色甲将前往测试枢纽站。",
                "如果路线被封，他可能改去旧港。",
            ]
        )
        MockModelHandler.deltas = [
            {
                "category": "location",
                "subject": "测试角色甲",
                "field": "location",
                "operation": "set",
                "value": "测试城南站候车厅",
                "confidence": 0.98,
                "evidence": "测试角色甲已经抵达测试城，落脚在南站候车厅。",
            },
            {
                "category": "inventory",
                "subject": "测试角色甲",
                "field": "inventory",
                "operation": "set",
                "value": {"item": "青铜钥匙", "status": "held"},
                "confidence": 0.97,
                "evidence": "青铜钥匙仍由测试角色甲持有。",
            },
            {
                "category": "inventory",
                "subject": "测试角色甲",
                "field": "inventory",
                "operation": "set",
                "value": {"item": "列车通行证", "status": "held"},
                "confidence": 0.97,
                "evidence": "列车通行证也由测试角色甲持有。",
            },
            {
                "category": "relationship",
                "subject": "测试角色甲",
                "field": "relationship",
                "operation": "set",
                "value": {"target": "测试角色丁", "type": "盟友"},
                "confidence": 0.96,
                "evidence": "测试角色丁与测试角色甲正式结成盟友。",
            },
            {
                "category": "relationship",
                "subject": "测试角色甲",
                "field": "relationship",
                "operation": "set",
                "value": {"target": "测试角色戊", "type": "债务关系"},
                "confidence": 0.96,
                "evidence": "测试角色戊与测试角色甲形成债务关系。",
            },
            {
                "category": "story_time",
                "subject": "故事",
                "field": "time",
                "operation": "set",
                "value": "景历十二年三月初七夜",
                "confidence": 0.99,
                "evidence": "此刻是景历十二年三月初七夜。",
            },
            {
                "category": "location",
                "subject": "测试角色甲",
                "field": "current",
                "operation": "set",
                "scope": "current",
                "effective_at": "下一章",
                "value": "测试枢纽站",
                "confidence": 0.95,
                "evidence": "下一章测试角色甲将前往测试枢纽站。",
            },
            {
                "category": "location",
                "subject": "测试角色甲",
                "field": "current",
                "operation": "set",
                "value": "旧港",
                "confidence": 0.9,
                "evidence": "如果路线被封，他可能改去旧港。",
            },
        ]

        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            first = self.run_hook(
                root,
                {
                    "cwd": str(root),
                    "hook_event_name": "UserPromptSubmit",
                    "model": "test",
                    "permission_mode": "default",
                    "prompt": "继续推进剧情，安排测试角色甲抵达测试城后的行动",
                    "session_id": "session-e2e",
                    "transcript_path": None,
                    "turn_id": "turn-1",
                },
            )
            self.assertEqual(0, first.returncode, first.stderr)
            hook_output = json.loads(first.stdout)
            context = hook_output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("[STATE_RAG_RECEIPT]", context)
            self.assertIn("[CRAFT_RAG_GUIDANCE]", context)
            self.assertIn("渐进困境循环", context)
            self.assertIn("不得机械套模板", context)
            self.assertIn("继续推进剧情，安排测试角色甲抵达测试城后的行动", context)
            receipt_match = re.search(r"state_receipt_id: (srr-[a-f0-9]+)", context)
            self.assertIsNotNone(receipt_match, context)
            receipt_id = receipt_match.group(1)

            stopped = self.run_hook(
                root,
                {
                    "cwd": str(root),
                    "hook_event_name": "Stop",
                    "last_assistant_message": assistant_text,
                    "model": "test",
                    "permission_mode": "default",
                    "session_id": "session-e2e",
                    "stop_hook_active": False,
                    "transcript_path": None,
                    "turn_id": "turn-1",
                },
                stop=True,
            )
            self.assertEqual(0, stopped.returncode, stopped.stderr)
            stop_output = json.loads(stopped.stdout)
            self.assertIn("recorded_events=7", stop_output["systemMessage"])

            with closing(sqlite3.connect(root / ".plot-rag" / "state.sqlite3")) as connection:
                craft_receipt = json.loads(
                    connection.execute(
                        "SELECT craft_json FROM turns WHERE receipt_id=?", (receipt_id,)
                    ).fetchone()[0]
                )
                schema_version = connection.execute(
                    "SELECT value FROM state_meta WHERE key='schema_version'"
                ).fetchone()[0]
            self.assertEqual("2", schema_version)
            self.assertGreaterEqual(craft_receipt["methods_count"], 1)
            self.assertIn("continuation", craft_receipt["detected_tasks"])

            state = dump_state(root)
            self.assertEqual(6, state["facts_count"])
            self.assertEqual(7, state["events_count"])
            fields = {fact["field"] for fact in state["facts"]}
            self.assertTrue(
                {"current", "item:青铜钥匙", "item:列车通行证", "to:测试角色丁", "to:测试角色戊"}.issubset(fields)
            )
            self.assertEqual(1, sum(event["scope"] == "planned" for event in state["events"]))
            self.assertNotIn("旧港", json.dumps(state["facts"], ensure_ascii=False))
            self.assertTrue((root / ".plot-rag" / "state_snapshot.json").is_file())
            self.assertTrue((root / ".plot-rag" / "commits").is_dir())
            artifact_path = next((root / ".plot-rag" / "commits").glob("*.json"))
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertTrue(artifact["craft_trace"]["method_ids"])
            self.assertIn("continuation", artifact["craft_trace"]["detected_tasks"])

            idempotent = commit_turn(root, assistant_text, request_id=receipt_id)
            self.assertTrue(idempotent["idempotent"])
            self.assertEqual(1, MockModelHandler.chat_calls)

            conflict = commit_turn(root, assistant_text + "\n异文。", request_id=receipt_id)
            self.assertEqual("failed", conflict["status"])
            self.assertIn("different assistant_text", conflict["reason"])
            self.assertEqual(1, MockModelHandler.chat_calls)

            runtime_config = state_runtime._load_runtime_config(root)
            state_runtime._mark_turn_failed(
                runtime_config,
                idempotent["request_id"],
                receipt_id,
                "late concurrent failure",
                idempotent["remote"],
            )
            with closing(sqlite3.connect(root / ".plot-rag" / "state.sqlite3")) as connection:
                turn_status = connection.execute(
                    "SELECT status FROM turns WHERE receipt_id=?", (receipt_id,)
                ).fetchone()[0]
            self.assertEqual("committed", turn_status)

            second = self.run_hook(
                root,
                {
                    "cwd": str(root),
                    "hook_event_name": "UserPromptSubmit",
                    "model": "test",
                    "permission_mode": "default",
                    "prompt": "继续推进剧情，先核对测试角色甲现在的位置和所持道具",
                    "session_id": "session-e2e",
                    "transcript_path": None,
                    "turn_id": "turn-2",
                },
            )
            self.assertEqual(0, second.returncode, second.stderr)
            second_context = json.loads(second.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("测试城南站候车厅", second_context)
            self.assertIn("青铜钥匙", second_context)
            self.assertGreaterEqual(MockModelHandler.embedding_calls, 2)
            self.assertGreaterEqual(MockModelHandler.rerank_calls, 1)

    def test_craft_retrieval_adapts_methods_to_plot_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            climax = query_craft(
                root,
                "设计终局危机与高潮，让主角作出不可撤回的两难选择并从结局倒推铺垫",
                top_k=3,
            )
            scene = query_craft(
                root,
                "写下一章谈判场景，用信息差和对话冲突制造转折与离场钩子",
                top_k=3,
            )

        self.assertEqual("ready", climax["status"])
        self.assertEqual("ready", scene["status"])
        self.assertIn("climax", climax["detected_tasks"])
        self.assertIn("scene", scene["detected_tasks"])
        climax_ids = {method["id"] for method in climax["methods"]}
        scene_ids = {method["id"] for method in scene["methods"]}
        self.assertIn("crisis-climax-backchain", climax_ids)
        self.assertTrue({"scene-value-turn", "information-as-weapon"} & scene_ids)
        self.assertNotEqual(climax_ids, scene_ids)
        self.assertIn("[CRAFT_METHOD:", climax["context"])
        self.assertTrue(all(method["source"]["path"].startswith("写作指南/") for method in climax["methods"]))
        self.assertEqual("ok", climax["remote"]["embedding"]["status"])
        self.assertEqual("ok", climax["remote"]["rerank"]["status"])
        self.assertGreaterEqual(MockModelHandler.embedding_calls, 2)
        self.assertGreaterEqual(MockModelHandler.rerank_calls, 2)

    def test_craft_catalog_version_requires_exact_json_integer(self) -> None:
        payload = json.loads(
            state_runtime.CRAFT_CATALOG_PATH.read_text(encoding="utf-8-sig")
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, malformed in enumerate((True, 1.0), start=1):
                with self.subTest(version=repr(malformed)):
                    candidate = dict(payload)
                    candidate["version"] = malformed
                    path = root / f"craft-catalog-version-{index}.json"
                    path.write_text(
                        json.dumps(candidate, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    with (
                        patch.object(
                            state_runtime,
                            "CRAFT_CATALOG_PATH",
                            path,
                        ),
                        self.assertRaisesRegex(
                            state_runtime.StateRagError,
                            "unsupported schema version",
                        ),
                    ):
                        state_runtime._load_craft_catalog()

    def test_runtime_integer_limits_require_exact_integers(self) -> None:
        for malformed in (True, 1.0, "1"):
            with self.subTest(value=repr(malformed)):
                with self.assertRaisesRegex(
                    state_runtime.StateRagError,
                    "must be an integer",
                ):
                    state_runtime._bounded_int(
                        malformed,
                        1,
                        1,
                        8,
                        "query.top_k",
                    )

    def test_stop_without_prepared_turn_is_silent_and_does_not_call_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            completed = self.run_hook(
                root,
                {
                    "cwd": str(root),
                    "hook_event_name": "Stop",
                    "last_assistant_message": "普通分析回答。",
                    "model": "test",
                    "permission_mode": "default",
                    "session_id": "no-prepare",
                    "stop_hook_active": False,
                    "transcript_path": None,
                    "turn_id": "turn-x",
                },
                stop=True,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("", completed.stdout)
            self.assertEqual(0, MockModelHandler.chat_calls)
            self.assertFalse((root / ".plot-rag" / "state.sqlite3").exists())

    def test_stop_does_not_reuse_pending_receipt_from_another_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_turn(
                root,
                "继续推进剧情",
                session_id="shared-session",
                turn_id="turn-old",
            )
            self.assertEqual("ready", prepared["status"])
            completed = self.run_hook(
                root,
                {
                    "cwd": str(root),
                    "hook_event_name": "Stop",
                    "last_assistant_message": "这是另一个无关回合的回答。",
                    "model": "test",
                    "permission_mode": "default",
                    "session_id": "shared-session",
                    "stop_hook_active": False,
                    "transcript_path": None,
                    "turn_id": "turn-new",
                },
                stop=True,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("", completed.stdout)
            self.assertEqual(0, MockModelHandler.chat_calls)
            state = dump_state(root)
            self.assertEqual(0, state["events_count"])

    def test_invalid_evidence_fails_without_state_mutation(self) -> None:
        MockModelHandler.deltas = [
            {
                "category": "location",
                "subject": "测试角色甲",
                "field": "current",
                "operation": "set",
                "value": "不存在的地点",
                "confidence": 0.99,
                "evidence": "这段证据不在正文里。",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_turn(
                root,
                "继续推进剧情",
                session_id="bad-evidence",
                turn_id="turn-bad",
            )
            result = commit_turn(
                root,
                "测试角色甲留在原地。",
                request_id=prepared["receipt_id"],
            )
            self.assertEqual("failed", result["status"])
            state = dump_state(root)
            self.assertEqual(0, state["facts_count"])
            self.assertEqual(0, state["events_count"])

    def test_untrusted_remote_host_is_rejected_before_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            config_path = root / ".plot-rag" / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["remote"]["extract"]["base_url"] = (
                f"http://127.0.0.2:{self.server.server_port}/v1"
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")
            prepared = prepare_turn(
                root,
                "继续推进剧情",
                session_id="untrusted-host",
                turn_id="turn-1",
            )
            result = commit_turn(
                root,
                "测试角色甲留在原地。",
                request_id=prepared["receipt_id"],
            )
            self.assertEqual("failed", result["status"])
            self.assertIn("PLOT_RAG_TRUSTED_HOSTS", result["reason"])
            self.assertEqual(0, MockModelHandler.chat_calls)

    def test_shared_provider_key_is_bound_to_its_provider_host(self) -> None:
        service = state_runtime.ServiceConfig(
            name="rerank",
            enabled=True,
            base_url="https://api.jina.ai/v1",
            model="mock-rerank",
            api_key_env="SILICONFLOW_API_KEY",
            api_key_required=True,
            endpoint="rerank",
            timeout_seconds=1.0,
            max_tokens=2400,
        )
        with patch.dict(
            os.environ,
            {"SILICONFLOW_API_KEY": "TEST_SHARED_PROVIDER_KEY"},
            clear=False,
        ):
            readiness = state_runtime._service_readiness(service)
            with self.assertRaisesRegex(
                state_runtime.StateRagError,
                "restricted to api.siliconflow.cn",
            ):
                state_runtime._remote_json(
                    service,
                    {
                        "model": service.model,
                        "query": "q",
                        "documents": ["d"],
                        "top_n": 1,
                    },
                )
        self.assertEqual("unconfigured", readiness["status"])
        self.assertFalse(readiness["url_policy_ok"])
        self.assertIn("service-specific key", readiness["reason"])

    def test_non_https_remote_transport_is_rejected_before_request(self) -> None:
        service = state_runtime.ServiceConfig(
            name="extract",
            enabled=True,
            base_url="http://api.siliconflow.cn/v1",
            model="mock-chat",
            api_key_env="SILICONFLOW_API_KEY",
            api_key_required=True,
            endpoint="chat/completions",
            timeout_seconds=1.0,
            max_tokens=2400,
        )
        with patch.dict(
            os.environ,
            {"SILICONFLOW_API_KEY": "TEST_SHARED_PROVIDER_KEY"},
            clear=False,
        ):
            readiness = state_runtime._service_readiness(service)
            with self.assertRaisesRegex(
                state_runtime.StateRagError,
                "HTTPS is required",
            ):
                state_runtime._remote_json(service, {"model": service.model})
        self.assertEqual("unconfigured", readiness["status"])
        self.assertTrue(readiness["host_trusted"])
        self.assertFalse(readiness["url_policy_ok"])

    def test_remote_requests_reuse_keep_alive_connection_and_fixed_headers(
        self,
    ) -> None:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            KeepAliveRemoteHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state_runtime._REMOTE_CONNECTION_POOL.close_all()
        KeepAliveRemoteHandler.client_ports = []
        KeepAliveRemoteHandler.user_agents = []
        KeepAliveRemoteHandler.authorization_headers = []
        service = state_runtime.ServiceConfig(
            name="extract",
            enabled=True,
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="mock-chat",
            api_key_env="PLOT_RAG_KEEP_ALIVE_KEY",
            api_key_required=True,
            endpoint="chat/completions",
            timeout_seconds=2.0,
            max_tokens=2400,
        )
        try:
            with patch.dict(
                os.environ,
                {"PLOT_RAG_KEEP_ALIVE_KEY": "TEST_KEEP_ALIVE_KEY"},
                clear=False,
            ):
                first, first_status = state_runtime._remote_json(
                    service,
                    {"model": service.model, "messages": []},
                )
                second, second_status = state_runtime._remote_json(
                    service,
                    {"model": service.model, "messages": []},
                )
        finally:
            state_runtime._REMOTE_CONNECTION_POOL.close_all()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual({"ok": True}, first)
        self.assertEqual({"ok": True}, second)
        self.assertEqual("ok", first_status["status"])
        self.assertEqual("ok", second_status["status"])
        self.assertEqual(2, len(KeepAliveRemoteHandler.client_ports))
        self.assertEqual(
            1,
            len(set(KeepAliveRemoteHandler.client_ports)),
        )
        self.assertEqual(
            [state_runtime._REMOTE_USER_AGENT] * 2,
            KeepAliveRemoteHandler.user_agents,
        )
        self.assertEqual(
            ["Bearer TEST_KEEP_ALIVE_KEY"] * 2,
            KeepAliveRemoteHandler.authorization_headers,
        )

    def test_remote_429_retries_once_and_reports_attempts(self) -> None:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            RetrySequenceRemoteHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state_runtime._REMOTE_CONNECTION_POOL.close_all()
        state_runtime._REMOTE_RETRY_COORDINATOR.clear_all()
        RetrySequenceRemoteHandler.statuses = [429, 200]
        RetrySequenceRemoteHandler.retry_after = ["0", None]
        RetrySequenceRemoteHandler.calls = 0
        service = state_runtime.ServiceConfig(
            name="extract",
            enabled=True,
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="mock-chat",
            api_key_env="PLOT_RAG_RETRY_KEY",
            api_key_required=False,
            endpoint="chat/completions",
            timeout_seconds=3.0,
            max_tokens=2400,
        )
        try:
            value, status = state_runtime._remote_json(
                service,
                {"model": service.model, "request_id": "single"},
            )
        finally:
            state_runtime._REMOTE_CONNECTION_POOL.close_all()
            state_runtime._REMOTE_RETRY_COORDINATOR.clear_all()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual({"ok": True}, value)
        self.assertEqual(2, RetrySequenceRemoteHandler.calls)
        self.assertEqual(2, status["attempts"])
        self.assertEqual(1, status["retry_count"])
        self.assertEqual(200, status["http_status"])

    def test_remote_persistent_429_stops_at_three_attempts(self) -> None:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            RetrySequenceRemoteHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state_runtime._REMOTE_CONNECTION_POOL.close_all()
        state_runtime._REMOTE_RETRY_COORDINATOR.clear_all()
        RetrySequenceRemoteHandler.statuses = [429]
        RetrySequenceRemoteHandler.retry_after = ["0"]
        RetrySequenceRemoteHandler.calls = 0
        service = state_runtime.ServiceConfig(
            name="extract",
            enabled=True,
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="mock-chat",
            api_key_env="PLOT_RAG_RETRY_KEY",
            api_key_required=False,
            endpoint="chat/completions",
            timeout_seconds=3.0,
            max_tokens=2400,
        )
        try:
            with self.assertRaisesRegex(
                state_runtime.StateRagError,
                r"remote extract HTTP 429 after 3 attempts",
            ):
                state_runtime._remote_json(
                    service,
                    {"model": service.model, "request_id": "persistent"},
                )
        finally:
            state_runtime._REMOTE_CONNECTION_POOL.close_all()
            state_runtime._REMOTE_RETRY_COORDINATOR.clear_all()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(3, RetrySequenceRemoteHandler.calls)

    def test_remote_retry_coordinator_serializes_concurrent_probes(self) -> None:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            CoordinatedRetryRemoteHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state_runtime._REMOTE_CONNECTION_POOL.close_all()
        state_runtime._REMOTE_RETRY_COORDINATOR.clear_all()
        CoordinatedRetryRemoteHandler.first_wave = threading.Barrier(4)
        CoordinatedRetryRemoteHandler.counts = {}
        CoordinatedRetryRemoteHandler.calls = 0
        CoordinatedRetryRemoteHandler.active_retry = 0
        CoordinatedRetryRemoteHandler.max_retry_active = 0
        service = state_runtime.ServiceConfig(
            name="extract",
            enabled=True,
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="mock-chat",
            api_key_env="PLOT_RAG_RETRY_KEY",
            api_key_required=False,
            endpoint="chat/completions",
            timeout_seconds=8.0,
            max_tokens=2400,
        )
        errors: list[BaseException] = []
        results: list[dict[str, Any]] = []
        result_lock = threading.Lock()

        def invoke(index: int) -> None:
            try:
                value, _status = state_runtime._remote_json(
                    service,
                    {
                        "model": service.model,
                        "request_id": f"concurrent-{index}",
                    },
                )
                with result_lock:
                    results.append(value)
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)

        workers = [
            threading.Thread(target=invoke, args=(index,), daemon=True)
            for index in range(4)
        ]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
        finally:
            for worker in workers:
                worker.join(timeout=10)
            state_runtime._REMOTE_CONNECTION_POOL.close_all()
            state_runtime._REMOTE_RETRY_COORDINATOR.clear_all()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual([], errors)
        self.assertEqual(4, len(results))
        self.assertEqual(8, CoordinatedRetryRemoteHandler.calls)
        self.assertEqual(1, CoordinatedRetryRemoteHandler.max_retry_active)

    def test_retry_after_accepts_seconds_dates_and_bounded_fallback(self) -> None:
        self.assertEqual(
            7.0,
            state_runtime._remote_retry_delay_seconds(
                {"Retry-After": "7"},
                retry_index=1,
            ),
        )
        date_header = email.utils.format_datetime(
            datetime.fromtimestamp(1_000, timezone.utc)
            + timedelta(seconds=4),
            usegmt=True,
        )
        self.assertAlmostEqual(
            4.0,
            state_runtime._remote_retry_delay_seconds(
                {"Retry-After": date_header},
                retry_index=1,
                now_epoch_seconds=1_000,
            ),
            places=6,
        )
        self.assertEqual(
            state_runtime._REMOTE_RETRY_BASE_SECONDS,
            state_runtime._remote_retry_delay_seconds(
                {"Retry-After": "not-a-delay"},
                retry_index=1,
            ),
        )
        self.assertEqual(
            state_runtime._REMOTE_RETRY_MAX_BACKOFF_SECONDS,
            state_runtime._remote_retry_delay_seconds(
                {"Retry-After": "not-a-delay"},
                retry_index=10,
            ),
        )
        self.assertEqual(
            state_runtime._REMOTE_RETRY_AFTER_MAX_SECONDS,
            state_runtime._remote_retry_delay_seconds(
                {"Retry-After": "999999"},
                retry_index=1,
            ),
        )

    def test_remote_pool_enforces_per_service_concurrency_limit(self) -> None:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            BoundedConcurrencyRemoteHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state_runtime._REMOTE_CONNECTION_POOL.close_all()
        BoundedConcurrencyRemoteHandler.release_requests = threading.Event()
        BoundedConcurrencyRemoteHandler.concurrency_reached = threading.Event()
        BoundedConcurrencyRemoteHandler.calls = 0
        BoundedConcurrencyRemoteHandler.active = 0
        BoundedConcurrencyRemoteHandler.max_active = 0
        service = state_runtime.ServiceConfig(
            name="rerank",
            enabled=True,
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="mock-rerank",
            api_key_env="PLOT_RAG_UNUSED_TEST_KEY",
            api_key_required=False,
            endpoint="rerank",
            timeout_seconds=8.0,
            max_tokens=2400,
        )
        errors: list[BaseException] = []
        results: list[dict[str, Any]] = []
        result_lock = threading.Lock()

        def invoke_remote() -> None:
            try:
                value, _status = state_runtime._remote_json(
                    service,
                    {
                        "model": service.model,
                        "query": "q",
                        "documents": ["d"],
                    },
                )
                with result_lock:
                    results.append(value)
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)

        workers = [
            threading.Thread(target=invoke_remote, daemon=True)
            for _index in range(
                state_runtime._REMOTE_MAX_CONNECTIONS_PER_SERVICE * 2
            )
        ]
        try:
            for worker in workers:
                worker.start()
            self.assertTrue(
                BoundedConcurrencyRemoteHandler.concurrency_reached.wait(timeout=5)
            )
            self.assertEqual(
                state_runtime._REMOTE_MAX_CONNECTIONS_PER_SERVICE,
                BoundedConcurrencyRemoteHandler.max_active,
            )
            BoundedConcurrencyRemoteHandler.release_requests.set()
            for worker in workers:
                worker.join(timeout=10)
        finally:
            BoundedConcurrencyRemoteHandler.release_requests.set()
            for worker in workers:
                worker.join(timeout=10)
            state_runtime._REMOTE_CONNECTION_POOL.close_all()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual([], errors)
        self.assertEqual(len(workers), len(results))
        self.assertEqual(len(workers), BoundedConcurrencyRemoteHandler.calls)
        self.assertLessEqual(
            BoundedConcurrencyRemoteHandler.max_active,
            state_runtime._REMOTE_MAX_CONNECTIONS_PER_SERVICE,
        )

    def test_remote_redirect_is_blocked_without_forwarding_credentials(
        self,
    ) -> None:
        sink = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            RedirectCredentialSinkHandler,
        )
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()
        redirect = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            RedirectingRemoteHandler,
        )
        redirect_thread = threading.Thread(
            target=redirect.serve_forever,
            daemon=True,
        )
        redirect_thread.start()
        RedirectCredentialSinkHandler.calls = 0
        RedirectCredentialSinkHandler.authorization_headers = []
        RedirectingRemoteHandler.authorization_headers = []
        RedirectingRemoteHandler.location = (
            f"http://localhost:{sink.server_port}/v1/chat/completions"
        )
        service = state_runtime.ServiceConfig(
            name="extract",
            enabled=True,
            base_url=f"http://127.0.0.1:{redirect.server_port}/v1",
            model="mock-chat",
            api_key_env="PLOT_RAG_LLM_API_KEY",
            api_key_required=True,
            endpoint="chat/completions",
            timeout_seconds=2.0,
            max_tokens=2400,
        )
        try:
            with patch.dict(
                os.environ,
                {"PLOT_RAG_LLM_API_KEY": "TEST_REDIRECT_KEY"},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    state_runtime.StateRagError,
                    "redirects are blocked",
                ):
                    state_runtime._remote_json(
                        service,
                        {"model": service.model, "messages": []},
                    )
        finally:
            redirect.shutdown()
            redirect.server_close()
            redirect_thread.join(timeout=5)
            sink.shutdown()
            sink.server_close()
            sink_thread.join(timeout=5)
        self.assertEqual(
            ["Bearer TEST_REDIRECT_KEY"],
            RedirectingRemoteHandler.authorization_headers,
        )
        self.assertEqual(0, RedirectCredentialSinkHandler.calls)
        self.assertEqual(
            [],
            RedirectCredentialSinkHandler.authorization_headers,
        )

    def test_doctor_and_dump_report_missing_database_without_creating_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            before = self.tree_fingerprints(root)
            dumped = dump_state(root)
            health = doctor(root)
            after = self.tree_fingerprints(root)
            storage = self.state_storage_fingerprints(root)
        self.assertEqual(before, after)
        self.assertEqual("degraded", dumped["status"])
        self.assertEqual("database_not_created", dumped["reason"])
        self.assertEqual("not_created", dumped["storage"]["status"])
        self.assertEqual("degraded", health["status"])
        database_check = next(
            item for item in health["checks"] if item["name"] == "database"
        )
        self.assertEqual("not_created", database_check["status"])
        self.assertFalse(any(value[0] for value in storage.values()))

    def test_doctor_and_dump_leave_existing_database_and_sidecars_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            runtime_config = state_runtime._load_runtime_config(root)
            with state_runtime._open_database(runtime_config) as connection:
                connection.execute(
                    "UPDATE state_meta SET updated_at=? WHERE key='schema_version'",
                    ("sentinel-read-only",),
                )
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            database = root / ".plot-rag" / "state.sqlite3"
            wal = Path(str(database) + "-wal")
            shm = Path(str(database) + "-shm")
            self.assertTrue(database.is_file())
            self.assertFalse(wal.exists())
            self.assertFalse(shm.exists())
            before = self.state_storage_fingerprints(root)
            dumped = dump_state(root)
            health = doctor(root)
            after = self.state_storage_fingerprints(root)
        self.assertEqual(before, after)
        self.assertEqual(0, dumped["facts_count"])
        self.assertEqual(0, dumped["events_count"])
        database_check = next(
            item for item in health["checks"] if item["name"] == "database"
        )
        self.assertEqual("ok", database_check["status"])
        self.assertFalse(after["wal"][0])
        self.assertFalse(after["shm"][0])

    def test_doctor_and_dump_read_wal_only_state_without_touching_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            runtime_config = state_runtime._load_runtime_config(root)
            writer = state_runtime._open_database(runtime_config)
            try:
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute(
                    "UPDATE state_meta SET updated_at=? WHERE key='schema_version'",
                    ("sentinel-wal",),
                )
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.execute(
                    """
                    INSERT INTO turns(
                        receipt_id, request_id, session_id, turn_id, prompt,
                        prompt_hash, status, retrieved_json, authority_json,
                        craft_json, remote_json, result_json, error, started_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "receipt-wal",
                        "request-wal",
                        "session-wal",
                        "turn-wal",
                        "继续推进剧情",
                        "prompt-hash",
                        "pending",
                        "[]",
                        "{}",
                        "{}",
                        "{}",
                        "{}",
                        "",
                        "2026-07-16T00:00:00+00:00",
                    ),
                )
                writer.execute(
                    """
                    INSERT INTO state_events(
                        event_id, request_id, receipt_id, session_id, category,
                        subject, field, operation, scope, effective_at, value_json,
                        confidence, evidence, source_hash, created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "event-wal",
                        "request-wal",
                        "receipt-wal",
                        "session-wal",
                        "location",
                        "测试角色甲",
                        "current",
                        "set",
                        "current",
                        None,
                        json.dumps("测试城", ensure_ascii=False),
                        1.0,
                        "测试角色甲抵达测试城。",
                        "source-hash",
                        "2026-07-16T00:00:01+00:00",
                    ),
                )
                writer.execute(
                    """
                    INSERT INTO current_facts(
                        fact_key, category, subject, field, value_json, event_id,
                        effective_at, confidence, evidence, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "location:测试角色甲:current",
                        "location",
                        "测试角色甲",
                        "current",
                        json.dumps("测试城", ensure_ascii=False),
                        "event-wal",
                        None,
                        1.0,
                        "测试角色甲抵达测试城。",
                        "2026-07-16T00:00:01+00:00",
                    ),
                )
                writer.commit()
                before = self.state_storage_fingerprints(root)
                self.assertTrue(before["wal"][0])
                self.assertTrue(before["shm"][0])
                dumped = dump_state(root)
                health = doctor(root)
                after = self.state_storage_fingerprints(root)
            finally:
                writer.close()
        self.assertEqual(before, after)
        self.assertEqual(1, dumped["facts_count"])
        self.assertEqual(1, dumped["events_count"])
        self.assertEqual("测试城", dumped["facts"][0]["value"])
        self.assertEqual(1, health["facts_count"])
        self.assertEqual(1, health["events_count"])
        self.assertEqual(1, health["turns_count"])

    def test_alternative_response_does_not_update_current_state(self) -> None:
        MockModelHandler.deltas = [
            {
                "category": "location",
                "subject": "测试角色甲",
                "field": "current",
                "operation": "set",
                "value": "测试城",
                "confidence": 0.99,
                "evidence": "测试角色甲进入测试城。",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_project(Path(temporary))
            prepared = prepare_turn(
                root,
                "给我三个后续剧情方案",
                session_id="alternatives",
                turn_id="turn-1",
            )
            result = commit_turn(
                root,
                "方案一：测试角色甲进入测试城。\n方案二：测试角色甲留在城外。",
                request_id=prepared["receipt_id"],
            )
            self.assertEqual("committed", result["status"])
            self.assertEqual([], result["recorded_events"])
            self.assertTrue(
                any(item.get("reason") == "alternative_branch" for item in result["skipped"])
            )
            state = dump_state(root)
            self.assertEqual(0, state["facts_count"])
            self.assertEqual(0, state["events_count"])


class StateRagExtractionCoverageTests(unittest.TestCase):
    STATE_SENTENCE = "测试角色甲伤势稳定。"
    RELATION_SENTENCE = "测试角色甲与测试角色丙结为盟友。"
    INVENTORY_SENTENCE = "测试角色甲获得铜钥匙。"
    MOVEMENT_SENTENCE = "测试角色甲当前位于南站。"
    TIME_SENTENCE = "时间已是凌晨两点。"
    FIVE_FACT_TEXT = (
        STATE_SENTENCE
        + RELATION_SENTENCE
        + INVENTORY_SENTENCE
        + MOVEMENT_SENTENCE
        + TIME_SENTENCE
    )

    @staticmethod
    def make_v3_config(base: Path) -> state_runtime.RuntimeConfig:
        root = base / "novel"
        (root / ".plot-rag").mkdir(parents=True)
        for name in ("正文", "设定集", "剧情"):
            (root / name).mkdir()
        config = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )
        (root / ".plot-rag" / "config.json").write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )
        return state_runtime._load_runtime_config(root)

    @staticmethod
    def response(
        envelope: dict[str, Any] | str,
        *,
        finish_reason: str = "stop",
    ) -> dict[str, Any]:
        content = (
            envelope
            if isinstance(envelope, str)
            else json.dumps(envelope, ensure_ascii=False)
        )
        return {
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"content": content},
                }
            ]
        }

    @staticmethod
    def envelope(deltas: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_version": state_runtime.DELTA_V3_SCHEMA,
            "deltas": deltas,
        }

    @classmethod
    def delta(
        cls,
        event_type: str,
        *,
        evidence: str | None = None,
    ) -> dict[str, Any]:
        definitions: dict[str, dict[str, Any]] = {
            "state": {
                "action": "set",
                "subject": "测试角色甲",
                "object": None,
                "field": "injury",
                "value": "稳定",
                "evidence": cls.STATE_SENTENCE,
            },
            "relation": {
                "action": "set",
                "subject": "测试角色甲",
                "object": "测试角色丙",
                "field": "alliance",
                "value": {"kind": "盟友"},
                "evidence": cls.RELATION_SENTENCE,
            },
            "inventory": {
                "action": "acquire",
                "subject": "测试角色甲",
                "object": "铜钥匙",
                "field": "item",
                "value": {"quantity": 1},
                "evidence": cls.INVENTORY_SENTENCE,
            },
            "movement": {
                "action": "arrive",
                "subject": "测试角色甲",
                "object": "南站",
                "field": "current",
                "value": {},
                "evidence": cls.MOVEMENT_SENTENCE,
            },
            "time": {
                "action": "set",
                "subject": "故事",
                "object": None,
                "field": "current",
                "value": "凌晨两点",
                "effective_at": "凌晨两点",
                "evidence": cls.TIME_SENTENCE,
            },
            "world_rule": {
                "action": "set",
                "subject": "世界",
                "object": None,
                "field": "station_rule",
                "value": "车站按固定时刻封闭",
                "evidence": cls.STATE_SENTENCE,
            },
        }
        delta = {
            "event_type": event_type,
            **definitions[event_type],
            "scope": "current",
            "knowledge_plane": "objective",
            "confidence": 0.99,
        }
        if evidence is not None:
            delta["evidence"] = evidence
        return delta

    @classmethod
    def first_three_deltas(cls) -> list[dict[str, Any]]:
        return [
            cls.delta("state"),
            cls.delta("relation"),
            cls.delta("inventory"),
        ]

    @classmethod
    def five_deltas(cls) -> list[dict[str, Any]]:
        return cls.first_three_deltas() + [
            cls.delta("movement"),
            cls.delta("time"),
        ]

    def test_finish_reason_length_gets_one_complete_retry(self) -> None:
        assistant = self.STATE_SENTENCE
        repaired = self.envelope([self.delta("state")])
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_v3_config(Path(temporary))
            with patch(
                "state_rag._remote_json",
                side_effect=[
                    (
                        self.response(
                            '{"schema_version":"plot-rag-delta/v3","deltas":[',
                            finish_reason="length",
                        ),
                        {"status": "ok"},
                    ),
                    (self.response(repaired), {"status": "ok"}),
                ],
            ) as remote:
                deltas, skipped, status = state_runtime._chat_extract(
                    config,
                    assistant,
                    "记录测试角色甲当前伤势",
                    [],
                )

        self.assertEqual([], skipped)
        self.assertEqual(["state"], [delta["event_type"] for delta in deltas])
        self.assertEqual(2, remote.call_count)
        self.assertEqual(2, status["attempts"])
        self.assertTrue(status["repair_applied"])
        self.assertEqual("decode_or_truncation", status["repair_reason"])
        repair_system = remote.call_args_list[1].args[1]["messages"][0]["content"]
        self.assertIn("previous attempt was invalid or truncated", repair_system)

    def test_second_length_response_stops_after_two_remote_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_v3_config(Path(temporary))
            truncated = (
                self.response("{}", finish_reason="length"),
                {"status": "ok"},
            )
            with patch(
                "state_rag._remote_json",
                side_effect=[truncated, truncated],
            ) as remote:
                with self.assertRaisesRegex(
                    state_runtime.StateRagError,
                    "finish_reason is not stop: length",
                ):
                    state_runtime._chat_extract(
                        config,
                        self.STATE_SENTENCE,
                        "记录测试角色甲当前伤势",
                        [],
                    )
                self.assertEqual(2, remote.call_count)

    def test_missing_finish_reason_gets_one_strict_retry(self) -> None:
        repaired = self.envelope([self.delta("state")])
        missing_finish = self.response(repaired)
        del missing_finish["choices"][0]["finish_reason"]
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_v3_config(Path(temporary))
            with patch(
                "state_rag._remote_json",
                side_effect=[
                    (missing_finish, {"status": "ok"}),
                    (self.response(repaired), {"status": "ok"}),
                ],
            ) as remote:
                deltas, skipped, status = state_runtime._chat_extract(
                    config,
                    self.STATE_SENTENCE,
                    "记录测试角色甲当前伤势",
                    [],
                )

        self.assertEqual([], skipped)
        self.assertEqual(["state"], [delta["event_type"] for delta in deltas])
        self.assertEqual(2, remote.call_count)
        self.assertEqual("decode_or_truncation", status["repair_reason"])

    def test_validation_error_gets_one_complete_replacement_retry(self) -> None:
        invalid_schema = {
            "schema_version": "plot-rag-delta/invalid",
            "deltas": [],
        }
        repaired = self.envelope([self.delta("state")])
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_v3_config(Path(temporary))
            with patch(
                "state_rag._remote_json",
                side_effect=[
                    (self.response(invalid_schema), {"status": "ok"}),
                    (self.response(repaired), {"status": "ok"}),
                ],
            ) as remote:
                deltas, skipped, status = state_runtime._chat_extract(
                    config,
                    self.STATE_SENTENCE,
                    "记录测试角色甲当前伤势",
                    [],
                )
        self.assertEqual([], skipped)
        self.assertEqual(["state"], [delta["event_type"] for delta in deltas])
        self.assertEqual(2, remote.call_count)
        self.assertEqual(2, status["attempts"])
        self.assertTrue(status["repair_applied"])
        self.assertEqual("validation", status["repair_reason"])
        repair_system = remote.call_args_list[1].args[1]["messages"][0]["content"]
        repair_user = remote.call_args_list[1].args[1]["messages"][1]["content"]
        self.assertIn("complete legal replacement envelope", repair_system)
        self.assertIn("PREVIOUS_INVALID_ENVELOPE", repair_user)

    def test_second_validation_error_stops_after_two_remote_calls(self) -> None:
        invalid_schema = {
            "schema_version": "plot-rag-delta/invalid",
            "deltas": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_v3_config(Path(temporary))
            with patch(
                "state_rag._remote_json",
                side_effect=[
                    (self.response(invalid_schema), {"status": "ok"}),
                    (self.response(invalid_schema), {"status": "ok"}),
                ],
            ) as remote:
                with self.assertRaisesRegex(
                    state_runtime.StateRagError,
                    "schema_version=plot-rag-delta/v4",
                ):
                    state_runtime._chat_extract(
                        config,
                        self.STATE_SENTENCE,
                        "记录测试角色甲当前伤势",
                        [],
                    )
        self.assertEqual(2, remote.call_count)

    def test_coverage_retry_adds_only_missing_movement_and_time(self) -> None:
        first = self.envelope(self.first_three_deltas())
        supplement = self.envelope(
            [self.delta("movement"), self.delta("time")]
        )
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_v3_config(Path(temporary))
            with patch(
                "state_rag._remote_json",
                side_effect=[
                    (self.response(first), {"status": "ok"}),
                    (self.response(supplement), {"status": "ok"}),
                ],
            ) as remote:
                deltas, skipped, status = state_runtime._chat_extract(
                    config,
                    self.FIVE_FACT_TEXT,
                    "记录本轮五类连续性事实",
                    [],
                )

        self.assertEqual([], skipped)
        self.assertEqual(
            {"state", "relation", "inventory", "movement", "time"},
            {delta["event_type"] for delta in deltas},
        )
        self.assertEqual(5, len(deltas))
        self.assertEqual(2, remote.call_count)
        self.assertEqual(2, status["attempts"])
        self.assertEqual("coverage", status["repair_reason"])
        repair_payload = remote.call_args_list[1].args[1]
        repair_system = repair_payload["messages"][0]["content"]
        repair_user = repair_payload["messages"][1]["content"]
        self.assertIn("['movement', 'time']", repair_system)
        self.assertNotIn("'world_rule'", repair_system)
        self.assertIn("ALLOWED_UNITS", repair_user)
        self.assertIn(self.MOVEMENT_SENTENCE, repair_user)
        self.assertIn(self.TIME_SENTENCE, repair_user)

    def test_empty_first_extract_repairs_explicit_location(self) -> None:
        assistant = "测试角色甲已经抵达检修室。"
        movement = {
            "event_type": "movement",
            "action": "arrive",
            "subject": "测试角色甲",
            "object": "检修室",
            "field": "current",
            "value": {},
            "scope": "current",
            "knowledge_plane": "objective",
            "confidence": 0.99,
            "evidence": assistant,
        }
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_v3_config(Path(temporary))
            with patch(
                "state_rag._remote_json",
                side_effect=[
                    (self.response(self.envelope([])), {"status": "ok"}),
                    (
                        self.response(self.envelope([movement])),
                        {"status": "ok"},
                    ),
                ],
            ) as remote:
                deltas, skipped, status = state_runtime._chat_extract(
                    config,
                    assistant,
                    "记录测试角色甲当前所在位置",
                    [],
                )

        self.assertEqual([], skipped)
        self.assertEqual(["movement"], [delta["event_type"] for delta in deltas])
        self.assertEqual(2, remote.call_count)
        self.assertEqual("coverage", status["repair_reason"])

    def test_time_metaphor_does_not_create_coverage_obligation(self) -> None:
        assistant = "测试角色甲的眼睛如深夜般漆黑。"
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_v3_config(Path(temporary))
            with patch(
                "state_rag._remote_json",
                side_effect=[
                    (self.response(self.envelope([])), {"status": "ok"})
                ],
            ) as remote:
                deltas, skipped, status = state_runtime._chat_extract(
                    config,
                    assistant,
                    "记录本轮连续性事实",
                    [],
                )

        self.assertEqual([], deltas)
        self.assertEqual([], skipped)
        self.assertEqual(1, remote.call_count)
        self.assertEqual(1, status["attempts"])

    def test_complete_first_extraction_uses_one_remote_call(self) -> None:
        complete = self.envelope(self.five_deltas())
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_v3_config(Path(temporary))
            with patch(
                "state_rag._remote_json",
                side_effect=[(self.response(complete), {"status": "ok"})],
            ) as remote:
                deltas, skipped, status = state_runtime._chat_extract(
                    config,
                    self.FIVE_FACT_TEXT,
                    "记录本轮五类连续性事实",
                    [],
                )

        self.assertEqual([], skipped)
        self.assertEqual(5, len(deltas))
        self.assertEqual(1, remote.call_count)
        self.assertEqual(1, status["attempts"])
        self.assertNotIn("repair_applied", status)

    def test_invalid_coverage_supplements_fail_closed_with_two_call_cap(
        self,
    ) -> None:
        cases = {
            "unrelated_world_rule": {
                "deltas": [
                    self.delta("movement"),
                    self.delta("time"),
                    self.delta("world_rule"),
                ],
                "error": "EXTRACTION_COVERAGE_REPAIR_ADDED_UNRELATED_EVENT",
            },
            "evidence_outside_allowed_unit": {
                "deltas": [
                    self.delta(
                        "movement",
                        evidence=self.STATE_SENTENCE,
                    ),
                    self.delta("time"),
                ],
                "error": "EXTRACTION_COVERAGE_REPAIR_EVIDENCE_OUTSIDE_UNIT",
            },
            "still_missing_time": {
                "deltas": [self.delta("movement")],
                "error": "EXTRACTION_COVERAGE_INCOMPLETE",
            },
        }
        first = self.envelope(self.first_three_deltas())
        for label, case in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                config = self.make_v3_config(Path(temporary))
                supplement = self.envelope(case["deltas"])
                with patch(
                    "state_rag._remote_json",
                    side_effect=[
                        (self.response(first), {"status": "ok"}),
                        (self.response(supplement), {"status": "ok"}),
                    ],
                ) as remote:
                    with self.assertRaisesRegex(
                        state_runtime.StateRagError,
                        str(case["error"]),
                    ):
                        state_runtime._chat_extract(
                            config,
                            self.FIVE_FACT_TEXT,
                            "记录本轮五类连续性事实",
                            [],
                        )
                    self.assertLessEqual(remote.call_count, 2)
                    self.assertEqual(2, remote.call_count)

    def test_coverage_units_isolate_definite_clause_and_independent_time(
        self,
    ) -> None:
        assistant = (
            "测试角色甲当前位于南站，但如果封站，他将转去北门。"
            "时间已是凌晨两点。"
        )
        units = state_runtime._coverage_units(
            assistant,
            [{"subject": "测试角色甲"}],
        )

        self.assertEqual(2, len(units))
        self.assertEqual(
            [
                ("测试角色甲当前位于南站，", ["movement"]),
                ("时间已是凌晨两点。", ["time"]),
            ],
            [
                (unit["quote"], unit["event_types"])
                for unit in units
            ],
        )
        self.assertFalse(any("封站" in unit["quote"] for unit in units))
        self.assertFalse(any("北门" in unit["quote"] for unit in units))


class StateRagDeltaV4ItemExtractionTests(unittest.TestCase):
    @staticmethod
    def make_config(base: Path) -> state_runtime.RuntimeConfig:
        return StateRagExtractionCoverageTests.make_v3_config(base)

    @staticmethod
    def envelope(deltas: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_version": state_runtime.DELTA_V4_SCHEMA,
            "deltas": deltas,
        }

    @staticmethod
    def response(envelope: dict[str, Any]) -> dict[str, Any]:
        return StateRagExtractionCoverageTests.response(envelope)

    @staticmethod
    def candidate(
        event_type: str,
        action: str,
        *,
        evidence: str,
        subject_kind: str,
        subject_mention: str,
        objects: list[dict[str, str]] | None = None,
        changes: dict[str, Any] | None = None,
        scope: str = "current",
        knowledge_plane: str = "objective",
        ordinal: int = 17,
    ) -> dict[str, Any]:
        return {
            "event_type": event_type,
            "action": action,
            "subject": {
                "kind": subject_kind,
                "mention": subject_mention,
            },
            "objects": list(objects or []),
            "changes": dict(changes or {}),
            "scope": scope,
            "story_coordinate": {
                "calendar_id": "story-main",
                "ordinal": ordinal,
                "label": "本轮",
            },
            "knowledge_plane": knowledge_plane,
            "confidence": 0.99,
            "evidence": evidence,
        }

    @classmethod
    def valid_candidates(cls) -> list[dict[str, Any]]:
        spec_evidence = "临时通行牌被定义为一次性通行凭证。"
        instance_evidence = "临时通行牌甲依据临时通行牌完成实例化。"
        custody_evidence = "测试角色乙把临时通行牌甲交给测试角色甲保管。"
        runtime_evidence = "临时通行牌甲受损，耐久下降1点。"
        function_runtime_evidence = "临时通行牌甲的通行功能已启用。"
        use_evidence = "测试角色甲使用临时通行牌甲的通行功能打开闸门。"
        observation_evidence = "测试角色甲观察到临时通行牌甲会在闸门前发光。"
        correction_evidence = (
            "修正事件E-1：临时通行牌甲只是受损，耐久下降1点。"
        )
        replacement = cls.candidate(
            "item_runtime",
            "damage",
            evidence=correction_evidence,
            subject_kind="item_instance",
            subject_mention="临时通行牌甲",
            changes={"delta": {"durability": 1}},
        )
        return [
            cls.candidate(
                "item_spec",
                "define",
                evidence=spec_evidence,
                subject_kind="item_definition",
                subject_mention="临时通行牌",
                changes={
                    "definition": {
                        "item_kind": "credential",
                        "stack_policy": "non_stackable",
                        "uniqueness_policy": "ordinary",
                        "description": "一次性通行凭证",
                    }
                },
                scope="timeless",
            ),
            cls.candidate(
                "item_instance",
                "instantiate",
                evidence=instance_evidence,
                subject_kind="item_instance",
                subject_mention="临时通行牌甲",
                objects=[
                    {
                        "role": "item_definition",
                        "mention": "临时通行牌",
                    }
                ],
                changes={"attributes": {}},
            ),
            cls.candidate(
                "item_custody",
                "handover",
                evidence=custody_evidence,
                subject_kind="item_instance",
                subject_mention="临时通行牌甲",
                objects=[
                    {"role": "from_carrier", "mention": "测试角色乙"},
                    {"role": "to_carrier", "mention": "测试角色甲"},
                ],
                changes={"quantity": 1},
            ),
            cls.candidate(
                "item_runtime",
                "damage",
                evidence=runtime_evidence,
                subject_kind="item_instance",
                subject_mention="临时通行牌甲",
                changes={"delta": {"durability": 1}},
            ),
            cls.candidate(
                "item_function_runtime",
                "enable",
                evidence=function_runtime_evidence,
                subject_kind="item_instance",
                subject_mention="临时通行牌甲",
                objects=[{"role": "function", "mention": "通行功能"}],
            ),
            cls.candidate(
                "item_use",
                "use",
                evidence=use_evidence,
                subject_kind="item_instance",
                subject_mention="临时通行牌甲",
                objects=[
                    {"role": "actor", "mention": "测试角色甲"},
                    {"role": "function", "mention": "通行功能"},
                    {"role": "target", "mention": "闸门"},
                ],
                changes={
                    "delta": {"charges": 1},
                    "observed_effects": {"opened": True},
                },
            ),
            cls.candidate(
                "item_observation",
                "observe",
                evidence=observation_evidence,
                subject_kind="item_instance",
                subject_mention="临时通行牌甲",
                objects=[{"role": "observer", "mention": "测试角色甲"}],
                changes={"observation": {"effect": "发光"}},
                knowledge_plane="actor_belief",
            ),
            cls.candidate(
                "item_correction",
                "correct",
                evidence=correction_evidence,
                subject_kind="item_event",
                subject_mention="事件E-1",
                objects=[
                    {"role": "target_event", "mention": "事件E-1"},
                    {"role": "item", "mention": "临时通行牌甲"},
                ],
                changes={"replacement": replacement},
            ),
        ]

    def test_v4_accepts_all_item_families_and_splits_from_frozen_v3(
        self,
    ) -> None:
        state_evidence = "测试角色甲伤势稳定。"
        legacy = StateRagExtractionCoverageTests.delta(
            "state",
            evidence=state_evidence,
        )
        legacy["action"] = "state"
        items = self.valid_candidates()
        assistant = state_evidence + "".join(
            str(candidate["evidence"]) for candidate in items
        )
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_config(Path(temporary))
            normalized, skipped = state_runtime.validate_delta_v4_envelope(
                self.envelope([legacy, *items]),
                assistant,
                config,
            )

        self.assertEqual([], skipped)
        self.assertEqual(9, len(normalized))
        self.assertEqual("set", normalized[0]["action"])
        self.assertEqual(
            state_runtime.DELTA_V3_SCHEMA,
            normalized[0]["schema_version"],
        )
        self.assertEqual(
            set(state_runtime.ITEM_DELTA_EVENT_TYPES),
            {
                delta["event_type"]
                for delta in normalized
                if delta["schema_version"]
                == state_runtime.DELTA_V4_SCHEMA
            },
        )
        legacy_result, item_result = state_runtime.split_delta_v4_results(
            normalized
        )
        self.assertEqual([normalized[0]], legacy_result)
        self.assertEqual(normalized[1:], item_result)
        self.assertEqual(
            item_result[0],
            state_runtime.normalize_item_extraction_candidate(
                item_result[0],
                assistant,
            ),
        )
        self.assertTrue(
            all(
                set(delta)
                == {
                    "schema_version",
                    "event_type",
                    "action",
                    "subject",
                    "objects",
                    "changes",
                    "scope",
                    "effective_at",
                    "story_coordinate",
                    "knowledge_plane",
                    "ambiguity",
                    "confidence",
                    "evidence",
                }
                for delta in item_result
            )
        )

    def test_v3_item_semantics_remain_closed_and_explicit_v3_still_works(
        self,
    ) -> None:
        from dataclasses import replace

        item = self.valid_candidates()[2]
        state = StateRagExtractionCoverageTests.delta("state")
        assistant = (
            StateRagExtractionCoverageTests.STATE_SENTENCE
            + str(item["evidence"])
        )
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_config(Path(temporary))
            with self.assertRaisesRegex(
                state_runtime.StateRagError,
                "typed v3 required keys",
            ):
                state_runtime._validate_deltas(
                    {
                        "schema_version": state_runtime.DELTA_V3_SCHEMA,
                        "deltas": [item],
                    },
                    assistant,
                    config,
                )
            legacy, skipped = state_runtime._validate_deltas(
                {
                    "schema_version": state_runtime.DELTA_V3_SCHEMA,
                    "deltas": [state],
                },
                assistant,
                config,
            )
            with self.assertRaisesRegex(
                state_runtime.StateRagError,
                "requires config version >= 3",
            ):
                state_runtime._validate_deltas(
                    self.envelope([item]),
                    assistant,
                    replace(config, version=2),
                )
        self.assertEqual([], skipped)
        self.assertEqual(["state"], [delta["event_type"] for delta in legacy])

    def test_v4_rejects_remote_before_after_and_derived_counters(
        self,
    ) -> None:
        base = self.valid_candidates()[5]
        mutations = {
            "top_level_after": lambda value: value.update(
                {"after": {"charges": 0}}
            ),
            "nested_before_state": lambda value: value["changes"].update(
                {"observed_effects": {"before_state": {"charges": 1}}}
            ),
            "remaining_counter": lambda value: value["changes"]["delta"].update(
                {"remaining_charges": 0}
            ),
            "stable_id": lambda value: value["changes"].update(
                {"observed_effects": {"item_instance_id": "generated-id"}}
            ),
            "camel_item_id": lambda value: value["changes"].update(
                {"observed_effects": {"itemInstanceId": "generated-id"}}
            ),
            "camel_before_state": lambda value: value["changes"].update(
                {"observed_effects": {"beforeState": {"charges": 1}}}
            ),
            "camel_remaining_charges": lambda value: value[
                "changes"
            ].update(
                {"observed_effects": {"remainingCharges": 0}}
            ),
            "camel_computed_state": lambda value: value["changes"].update(
                {"observed_effects": {"computedState": {"charges": 0}}}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                candidate = json.loads(
                    json.dumps(base, ensure_ascii=False)
                )
                mutate(candidate)
                with self.assertRaises(state_runtime.StateRagError):
                    state_runtime.normalize_item_extraction_candidate(
                        candidate,
                        str(candidate["evidence"]),
                    )

    def test_v4_requires_contiguous_evidence_and_anchors_all_mentions(
        self,
    ) -> None:
        base = self.valid_candidates()[2]
        assistant = "测试角色乙把临时通行牌甲交出。随后测试角色甲接过它保管。"
        with self.assertRaisesRegex(
            state_runtime.StateRagError,
            "exact contiguous quote",
        ):
            state_runtime.normalize_item_extraction_candidate(
                base,
                assistant,
            )

        unanchored = json.loads(json.dumps(base, ensure_ascii=False))
        unanchored["evidence"] = "测试角色乙把临时通行牌甲交出。"
        with self.assertRaisesRegex(
            state_runtime.StateRagError,
            "mention is not anchored",
        ):
            state_runtime.normalize_item_extraction_candidate(
                unanchored,
                str(unanchored["evidence"]),
            )

    def test_v4_enforces_closed_fields_finite_numbers_and_integer_counters(
        self,
    ) -> None:
        runtime = self.valid_candidates()[3]
        cases: list[tuple[str, dict[str, Any], str]] = []

        unknown = json.loads(json.dumps(runtime, ensure_ascii=False))
        unknown["target"] = "闸门"
        cases.append(("closed", unknown, "closed v4 item candidate"))

        non_integer_coordinate = json.loads(
            json.dumps(runtime, ensure_ascii=False)
        )
        non_integer_coordinate["story_coordinate"]["ordinal"] = 17.0
        cases.append(("ordinal", non_integer_coordinate, "must be an integer"))

        cooldown = json.loads(json.dumps(runtime, ensure_ascii=False))
        cooldown["action"] = "activate"
        cooldown["changes"]["delta"] = {"cooldown": 1.5}
        cases.append(
            (
                "cooldown",
                cooldown,
                "unsupported fields for item_runtime.activate",
            )
        )

        infinite = json.loads(json.dumps(runtime, ensure_ascii=False))
        infinite["action"] = "charge"
        infinite["changes"]["delta"] = {"energy": float("inf")}
        cases.append(("finite", infinite, "finite"))

        for label, candidate, error in cases:
            with self.subTest(case=label), self.assertRaisesRegex(
                state_runtime.StateRagError,
                error,
            ):
                state_runtime.normalize_item_extraction_candidate(
                    candidate,
                    str(candidate["evidence"]),
                )

    def test_single_action_echo_repair_is_narrow_and_deterministic(
        self,
    ) -> None:
        assistant = (
            StateRagExtractionCoverageTests.STATE_SENTENCE
            + StateRagExtractionCoverageTests.TIME_SENTENCE
        )
        state = StateRagExtractionCoverageTests.delta("state")
        state["action"] = "state"
        time_delta = StateRagExtractionCoverageTests.delta("time")
        time_delta["action"] = "time"
        world_rule = StateRagExtractionCoverageTests.delta("world_rule")
        world_rule["action"] = "world_rule"
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_config(Path(temporary))
            repaired, _ = state_runtime._validate_deltas(
                {
                    "schema_version": state_runtime.DELTA_V3_SCHEMA,
                    "deltas": [state, time_delta, world_rule],
                },
                assistant,
                config,
            )
        self.assertEqual(
            ["set", "set", "set"],
            [value["action"] for value in repaired],
        )

        item = self.valid_candidates()[2]
        item["action"] = "item_custody"
        with self.assertRaisesRegex(
            state_runtime.StateRagError,
            "action is unsupported",
        ):
            state_runtime.normalize_item_extraction_candidate(
                item,
                str(item["evidence"]),
            )

    def test_chat_extract_requests_v4_and_returns_neutral_item_candidate(
        self,
    ) -> None:
        item = self.valid_candidates()[2]
        assistant = str(item["evidence"])
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_config(Path(temporary))
            with patch(
                "state_rag._remote_json",
                return_value=(
                    self.response(self.envelope([item])),
                    {"status": "ok"},
                ),
            ) as remote:
                deltas, skipped, status = state_runtime._chat_extract(
                    config,
                    assistant,
                    "记录临时通行牌的交接",
                    [],
                )
        self.assertEqual([], skipped)
        self.assertEqual(["item_custody"], [value["event_type"] for value in deltas])
        self.assertEqual(1, status["attempts"])
        system = remote.call_args.args[1]["messages"][0]["content"]
        self.assertIn("plot-rag-delta/v4", system)
        self.assertIn("Never emit before/after/current/remaining", system)
        self.assertIn("local reducer alone reads before state", system)
        self.assertIn(
            "Every item v4 candidate MUST include story_coordinate",
            system,
        )
        self.assertIn(
            "only from ASSISTANT_TEXT or trusted CURRENT_FACTS",
            system,
        )
        self.assertIn(
            "omit that item candidate; never invent a coordinate",
            system,
        )
        self.assertNotIn('"legacy_v3_delta"', system)
        self.assertNotIn('"item_v4_candidate"', system)

        def prompt_example(name: str) -> dict[str, Any]:
            begin = f"BEGIN_VALID_{name}_ENVELOPE_EXAMPLE "
            end = f" END_VALID_{name}_ENVELOPE_EXAMPLE"
            payload = system.split(begin, 1)[1].split(end, 1)[0]
            value = json.loads(payload)
            self.assertEqual(
                {
                    "schema_version": state_runtime.DELTA_V4_SCHEMA,
                    "deltas": value["deltas"],
                },
                value,
            )
            return value

        legacy_example = prompt_example("NON_ITEM")
        item_example = prompt_example("ITEM")
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_config(Path(temporary))
            legacy_normalized, legacy_skipped = (
                state_runtime.validate_delta_v4_envelope(
                    legacy_example,
                    "测试角色甲伤势稳定。",
                    config,
                )
            )
            item_evidence = str(item_example["deltas"][0]["evidence"])
            item_normalized, item_skipped = (
                state_runtime.validate_delta_v4_envelope(
                    item_example,
                    item_evidence,
                    config,
                )
            )
        self.assertEqual([], legacy_skipped)
        self.assertEqual(["state"], [value["event_type"] for value in legacy_normalized])
        self.assertEqual([], item_skipped)
        self.assertEqual(
            ["item_custody"],
            [value["event_type"] for value in item_normalized],
        )

    def test_protected_item_coordinate_validation_fails_closed_on_first_call(
        self,
    ) -> None:
        candidate = json.loads(
            json.dumps(self.valid_candidates()[3], ensure_ascii=False)
        )
        candidate["story_coordinate"]["ordinal"] = 17.0
        assistant = str(candidate["evidence"])
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_config(Path(temporary))
            with patch(
                "state_rag._remote_json",
                return_value=(
                    self.response(self.envelope([candidate])),
                    {"status": "ok"},
                ),
            ) as remote:
                with self.assertRaisesRegex(
                    state_runtime.StateRagError,
                    "story_coordinate.ordinal",
                ):
                    state_runtime._chat_extract(
                        config,
                        assistant,
                        "记录临时通行牌的运行状态",
                        [],
                    )
        self.assertEqual(1, remote.call_count)

    @staticmethod
    def resolver(
        mention: str,
        reference_type: str,
        role: str,
    ) -> dict[str, Any]:
        identifiers = {
            ("临时通行牌", "item_definition"): "item-definition-pass",
            ("临时通行牌甲", "item_instance"): "item-instance-pass-a",
            ("测试角色乙", "entity"): "character-ningyu",
            ("测试角色甲", "entity"): "character-testactora",
            ("通行功能", "item_function"): "item-function-pass",
            ("闸门", "entity"): "location-gate",
            ("事件E-1", "item_event"): "event-item-e1",
            ("临时通行牌甲", "item_subject"): "item-instance-pass-a",
            ("开门术", "ability"): "ability-open-gate",
            ("通行绑定", "item_function_binding"): "binding-pass",
        }
        value = identifiers.get((mention, reference_type))
        if value is None:
            return {
                "status": "UNRESOLVED",
                "mention": mention,
                "reference_type": reference_type,
                "role": role,
            }
        return {"status": "RESOLVED", "reference_id": value}

    def test_public_adapter_builds_validator_ready_typed_item_events(
        self,
    ) -> None:
        from continuity.validators import normalize_event

        context = {
            "branch_id": "main",
            "chapter_no": 7,
            "scene_index": 2,
            "narrative_mode": "linear",
            "receipt_id": "receipt-7",
            "assistant_sha256": "a" * 64,
            "artifact_id": "chapter-7",
        }
        candidates = self.valid_candidates()
        assistant = "".join(
            str(candidate["evidence"]) for candidate in candidates
        )
        event_types: list[str] = []
        for candidate in candidates:
            result = state_runtime.adapt_item_extraction_candidate(
                candidate,
                assistant,
                context,
                self.resolver,
            )
            self.assertTrue(result["ok"], result["issues"])
            self.assertEqual([], result["issues"])
            event = result["event"]
            self.assertIsInstance(event, dict)
            self.assertNotIn("before", event)
            self.assertNotIn("after", event)
            normalized = normalize_event(
                event,
                artifact_stage="final",
                branch_id="main",
                chapter_no=7,
                scene_index=2,
            )
            self.assertEqual(
                state_runtime.DELTA_V4_SCHEMA,
                normalized["schema_version"],
            )
            self.assertEqual(
                str(candidate["evidence"]),
                normalized["evidence"]["quote"],
            )
            event_types.append(normalized["event_type"])
        self.assertEqual(
            [
                "item_spec",
                "item_instance",
                "item_custody",
                "item_runtime",
                "item_function_runtime",
                "item_use",
                "item_observation",
                "item_correction",
            ],
            event_types,
        )

    def test_public_adapter_rejects_wrong_candidate_schema_before_projection(
        self,
    ) -> None:
        candidate = {
            "schema_version": state_runtime.DELTA_V3_SCHEMA,
            **self.valid_candidates()[2],
        }
        with self.assertRaisesRegex(
            state_runtime.StateRagError,
            "schema_version must be plot-rag-delta/v4",
        ):
            state_runtime.adapt_item_extraction_candidate(
                candidate,
                str(candidate["evidence"]),
                {"branch_id": "main", "chapter_no": 1, "scene_index": 0},
                self.resolver,
            )

    def test_public_adapter_aligns_custody_anchors_with_continuity_validator(
        self,
    ) -> None:
        evidence = "测试角色乙把临时通行牌甲交给测试角色甲保管。"
        custodian_only = self.candidate(
            "item_custody",
            "handover",
            evidence=evidence,
            subject_kind="item_instance",
            subject_mention="临时通行牌甲",
            objects=[
                {"role": "from_custodian", "mention": "测试角色乙"},
                {"role": "to_custodian", "mention": "测试角色甲"},
            ],
            changes={"quantity": 1},
        )
        result = state_runtime.adapt_item_extraction_candidate(
            custodian_only,
            evidence,
            {"branch_id": "main", "chapter_no": 1, "scene_index": 0},
            self.resolver,
        )
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual([], result["issues"])
        self.assertEqual(
            "character-testactora",
            result["event"]["to_custodian_entity_id"],
        )

        missing_destination = json.loads(
            json.dumps(custodian_only, ensure_ascii=False)
        )
        missing_destination["objects"] = [
            {"role": "from_custodian", "mention": "测试角色乙"}
        ]
        rejected = state_runtime.adapt_item_extraction_candidate(
            missing_destination,
            evidence,
            {"branch_id": "main", "chapter_no": 1, "scene_index": 0},
            self.resolver,
        )
        self.assertFalse(rejected["ok"])
        self.assertIsNone(rejected["event"])
        self.assertEqual(
            ["ITEM_CUSTODY_ANCHOR_REQUIRED"],
            [issue["code"] for issue in rejected["issues"]],
        )

    def test_public_adapter_returns_structured_missing_ability_bridge_issue(
        self,
    ) -> None:
        evidence = "临时通行牌的通行功能采用能力桥接。"
        candidate = self.candidate(
            "item_spec",
            "define",
            evidence=evidence,
            subject_kind="function_definition",
            subject_mention="通行功能",
            objects=[
                {
                    "role": "item_definition",
                    "mention": "临时通行牌",
                }
            ],
            changes={
                "definition": {
                    "effect_owner": "ability_bridge",
                    "description": "能力桥接",
                }
            },
            scope="timeless",
        )
        result = state_runtime.adapt_item_extraction_candidate(
            candidate,
            evidence,
            {"branch_id": "main", "chapter_no": 1, "scene_index": 0},
            self.resolver,
        )
        self.assertFalse(result["ok"])
        self.assertIsNone(result["event"])
        self.assertEqual(
            ["ITEM_ABILITY_BRIDGE_REQUIRED"],
            [issue["code"] for issue in result["issues"]],
        )
        self.assertEqual(
            "neutral_candidate_validator",
            result["issues"][0]["details"]["adapter_stage"],
        )

    def test_public_adapter_resolves_function_definition_and_binding(
        self,
    ) -> None:
        from continuity.validators import normalize_event

        function_evidence = "临时通行牌的通行功能授予开门术。"
        function_candidate = self.candidate(
            "item_spec",
            "define",
            evidence=function_evidence,
            subject_kind="function_definition",
            subject_mention="通行功能",
            objects=[
                {
                    "role": "item_definition",
                    "mention": "临时通行牌",
                },
                {"role": "ability", "mention": "开门术"},
            ],
            changes={
                "definition": {
                    "effect_owner": "ability_bridge",
                    "granted_abilities": ["开门术"],
                    "description": "授予开门术",
                }
            },
            scope="timeless",
        )
        binding_evidence = "通行绑定将通行功能绑定到临时通行牌甲。"
        binding_candidate = self.candidate(
            "item_spec",
            "define",
            evidence=binding_evidence,
            subject_kind="function_binding",
            subject_mention="通行绑定",
            objects=[
                {"role": "function", "mention": "通行功能"},
                {
                    "role": "item_instance",
                    "mention": "临时通行牌甲",
                },
            ],
            changes={"definition": {"enabled": True}},
            scope="timeless",
        )
        assistant = function_evidence + binding_evidence
        adapted = [
            state_runtime.adapt_item_extraction_candidate(
                candidate,
                assistant,
                {"branch_id": "main", "chapter_no": 1, "scene_index": 0},
                self.resolver,
            )
            for candidate in (function_candidate, binding_candidate)
        ]
        self.assertTrue(all(result["ok"] for result in adapted), adapted)
        function_event = normalize_event(
            adapted[0]["event"],
            artifact_stage="bootstrap",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
        )
        binding_event = normalize_event(
            adapted[1]["event"],
            artifact_stage="bootstrap",
            branch_id="main",
            chapter_no=1,
            scene_index=0,
        )
        self.assertEqual(
            ["ability-open-gate"],
            function_event["definition"]["granted_ability_ids"],
        )
        self.assertEqual(
            "item-definition-pass",
            function_event["definition"]["item_definition_id"],
        )
        self.assertEqual(
            "item-function-pass",
            binding_event["definition"]["function_id"],
        )
        self.assertEqual(
            "item-instance-pass-a",
            binding_event["definition"]["item_instance_id"],
        )

    def test_public_adapter_returns_structured_ambiguity_issues(
        self,
    ) -> None:
        candidate = self.valid_candidates()[2]

        def ambiguous_resolver(
            mention: str,
            reference_type: str,
            role: str,
        ) -> Any:
            if mention == "测试角色甲":
                return {
                    "status": "AMBIGUOUS",
                    "candidates": ["character-1", "character-2"],
                }
            return self.resolver(mention, reference_type, role)

        result = state_runtime.adapt_item_extraction_candidate(
            candidate,
            str(candidate["evidence"]),
            {"branch_id": "main", "chapter_no": 1, "scene_index": 0},
            ambiguous_resolver,
        )
        self.assertFalse(result["ok"])
        self.assertIsNone(result["event"])
        self.assertEqual(
            ["ITEM_REFERENCE_AMBIGUOUS"],
            [issue["code"] for issue in result["issues"]],
        )
        self.assertEqual(
            "to_carrier",
            result["issues"][0]["details"]["role"],
        )


class StateRagDeltaV4AdvantageExtractionTests(unittest.TestCase):
    @staticmethod
    def candidate(
        event_type: str,
        action: str,
        evidence: str,
        *,
        subject_kind: str,
        subject_mention: str,
        objects: list[dict[str, str]] | None = None,
        changes: dict[str, Any] | None = None,
        scope: str = "current",
        knowledge_plane: str = "objective",
        ordinal: int = 17,
    ) -> dict[str, Any]:
        return {
            "schema_version": state_runtime.DELTA_V4_SCHEMA,
            "event_type": event_type,
            "action": action,
            "subject": {
                "kind": subject_kind,
                "mention": subject_mention,
            },
            "objects": list(objects or []),
            "changes": dict(changes or {}),
            "scope": scope,
            "story_coordinate": {
                "calendar_id": "story-main",
                "ordinal": ordinal,
            },
            "knowledge_plane": knowledge_plane,
            "confidence": 0.99,
            "evidence": evidence,
            "effective_at": None,
            "ambiguity": None,
        }

    @classmethod
    def valid_candidates(cls) -> list[dict[str, Any]]:
        replacement_evidence = "样例优势核心真相是状态解析会揭示它吞噬异常能量。"
        replacement = cls.candidate(
            "advantage_reveal",
            "reveal",
            replacement_evidence,
            subject_kind="advantage_knowledge",
            subject_mention="样例优势核心真相",
            objects=[
                {"role": "advantage", "mention": "样例优势核心"},
                {"role": "module", "mention": "状态解析"},
            ],
            changes={
                "claim": {"fact": "样例优势核心会吞噬异常能量"},
                "reveal_stage": "corrected",
                "status": "canon",
            },
        )
        return [
            cls.candidate(
                "advantage_spec",
                "define",
                "样例优势核心在此被定义为金手指。",
                subject_kind="advantage_definition",
                subject_mention="样例优势核心",
                changes={
                    "title": "样例优势核心",
                    "profiles": ["resource_transformer"],
                    "anchor_type": "item_instance",
                    "acquisition_mode": "继承",
                    "uniqueness": "unique",
                    "promise": {},
                    "counterplay": {},
                    "definition": {},
                },
            ),
            cls.candidate(
                "advantage_anchor",
                "define",
                "样例优势核心通过示例核心载体锚定在示例核心上，归测试角色甲所有。",
                subject_kind="advantage_anchor",
                subject_mention="示例核心载体",
                objects=[
                    {"role": "advantage", "mention": "样例优势核心"},
                    {"role": "anchor_ref", "mention": "示例核心"},
                    {"role": "owner", "mention": "测试角色甲"},
                ],
                changes={
                    "anchor_type": "item_instance",
                    "binding_state": "unbound",
                    "transfer_rule": {},
                    "attributes": {},
                },
            ),
            cls.candidate(
                "advantage_module",
                "define",
                "样例优势核心的状态解析模块依托示例核心载体辨认异常能量。",
                subject_kind="advantage_module",
                subject_mention="状态解析",
                objects=[
                    {"role": "advantage", "mention": "样例优势核心"},
                    {"role": "anchor", "mention": "示例核心载体"},
                ],
                changes={
                    "title": "状态解析",
                    "kind": "appraisal",
                    "module_status": "available",
                    "stage": "initial",
                    "trigger": {},
                    "preconditions": [],
                    "targets": [],
                    "costs": {},
                    "effects": [],
                    "side_effects": [],
                    "failure_modes": [],
                    "counters": [],
                },
            ),
            cls.candidate(
                "advantage_bind",
                "bind",
                "测试角色甲把样例优势核心绑定到示例核心载体。",
                subject_kind="advantage",
                subject_mention="样例优势核心",
                objects=[
                    {"role": "anchor", "mention": "示例核心载体"},
                    {"role": "owner", "mention": "测试角色甲"},
                ],
            ),
            cls.candidate(
                "advantage_activate",
                "activate",
                "测试角色甲激活样例优势核心。",
                subject_kind="advantage",
                subject_mention="样例优势核心",
                objects=[{"role": "owner", "mention": "测试角色甲"}],
                changes={"stage": "active"},
            ),
            cls.candidate(
                "advantage_trigger",
                "trigger",
                "测试角色甲触发样例优势核心的状态解析模块。",
                subject_kind="advantage",
                subject_mention="样例优势核心",
                objects=[
                    {"role": "module", "mention": "状态解析"},
                    {"role": "actor", "mention": "测试角色甲"},
                ],
                changes={"effects": ["辨认异常能量"]},
            ),
            cls.candidate(
                "advantage_use",
                "use",
                "测试角色甲消耗一缕演算点，用样例优势核心的状态解析模块辨明异常能量。",
                subject_kind="advantage",
                subject_mention="样例优势核心",
                objects=[
                    {"role": "module", "mention": "状态解析"},
                    {"role": "actor", "mention": "测试角色甲"},
                ],
                changes={
                    "costs": {"演算点": 1},
                    "effects": ["辨明异常能量"],
                },
            ),
            cls.candidate(
                "advantage_reward",
                "reward",
                "样例优势核心奖励测试角色甲一缕演算点。",
                subject_kind="advantage",
                subject_mention="样例优势核心",
                objects=[{"role": "actor", "mention": "测试角色甲"}],
                changes={"rewards": {"演算点": 1}},
            ),
            cls.candidate(
                "advantage_cost",
                "cost",
                "样例优势核心让测试角色甲付出一缕演算点。",
                subject_kind="advantage",
                subject_mention="样例优势核心",
                objects=[{"role": "actor", "mention": "测试角色甲"}],
                changes={"costs": {"演算点": 1}},
            ),
            cls.candidate(
                "advantage_upgrade",
                "upgrade",
                "样例优势核心升级到二阶并解锁状态解析模块。",
                subject_kind="advantage",
                subject_mention="样例优势核心",
                objects=[{"role": "unlock_module", "mention": "状态解析"}],
                changes={"to_stage": "二阶", "max_charges": 3},
            ),
            cls.candidate(
                "advantage_reveal",
                "reveal",
                "测试角色甲得知样例优势核心真相，并确认状态解析会积累污染。",
                subject_kind="advantage_knowledge",
                subject_mention="样例优势核心真相",
                objects=[
                    {"role": "advantage", "mention": "样例优势核心"},
                    {"role": "module", "mention": "状态解析"},
                    {"role": "observer", "mention": "测试角色甲"},
                ],
                changes={
                    "claim": {"fact": "状态解析会积累误差"},
                    "reveal_stage": "first_reveal",
                    "status": "canon",
                    "record_ledger": True,
                },
                knowledge_plane="actor_belief",
            ),
            cls.candidate(
                "advantage_contract",
                "define",
                "测试角色甲与样例优势核心订立样例契约。",
                subject_kind="advantage_contract",
                subject_mention="样例契约",
                objects=[
                    {"role": "advantage", "mention": "样例优势核心"},
                    {"role": "actor", "mention": "测试角色甲"},
                ],
                changes={"terms": ["每次调用都要记录代价"]},
            ),
            cls.candidate(
                "advantage_correction",
                "correct",
                "事件E1需要校正。",
                subject_kind="advantage_event",
                subject_mention="事件E1",
                objects=[{"role": "target_event", "mention": "事件E1"}],
                changes={"replacement": replacement},
            ),
        ]

    @staticmethod
    def resolver(
        mention: str,
        reference_type: str,
        role: str,
    ) -> dict[str, Any]:
        identifiers = {
            ("样例优势核心", "advantage"): "advantage-sample-core",
            ("示例核心载体", "advantage_anchor"): "anchor-sample-core",
            ("示例核心", "item_instance"): "item-sample-core",
            ("状态解析", "advantage_module"): "module-inspect-sample",
            ("样例优势核心真相", "advantage_knowledge"): "knowledge-truth",
            ("样例契约", "advantage_contract"): "contract-sample-core",
            ("测试角色甲", "entity"): "actor-testactora",
            ("事件E1", "advantage_event"): "event-e1",
        }
        value = identifiers.get((mention, reference_type))
        if value is None:
            return {
                "status": "UNRESOLVED",
                "mention": mention,
                "reference_type": reference_type,
                "role": role,
            }
        return {"status": "RESOLVED", "reference_id": value}

    def test_all_advantage_candidate_families_normalize_and_adapt(self) -> None:
        candidates = self.valid_candidates()
        assistant = "".join(str(value["evidence"]) for value in candidates)
        assistant += str(
            candidates[-1]["changes"]["replacement"]["evidence"]
        )
        normalized = [
            state_runtime.normalize_advantage_extraction_candidate(
                value,
                assistant,
                index=index,
            )
            for index, value in enumerate(candidates)
        ]
        self.assertEqual(
            set(state_runtime.ADVANTAGE_DELTA_EVENT_TYPES),
            {value["event_type"] for value in normalized},
        )
        adapted = state_runtime.adapt_advantage_extraction_candidates(
            candidates,
            assistant,
            {
                "branch_id": "main",
                "chapter_no": 7,
                "scene_index": 2,
                "artifact_stage": "draft",
            },
            self.resolver,
        )
        self.assertTrue(adapted["ok"], adapted["issues"])
        self.assertEqual(13, adapted["adapted_count"])
        self.assertTrue(
            all(
                event["schema_version"]
                == state_runtime.ADVANTAGE_EVENT_SCHEMA
                and event["advantage_id"]
                for event in adapted["events"]
            )
        )
        upgrade = next(
            event
            for event in adapted["events"]
            if event["event_type"] == "advantage_upgrade"
        )
        reveal = next(
            event
            for event in adapted["events"]
            if event["event_type"] == "advantage_reveal"
        )
        correction = next(
            event
            for event in adapted["events"]
            if event["event_type"] == "advantage_correction"
        )
        self.assertNotIn("action", upgrade)
        self.assertNotIn("action", reveal)
        self.assertEqual("correct", correction["action"])

    def test_mixed_envelope_splits_legacy_item_and_advantage(self) -> None:
        legacy = StateRagExtractionCoverageTests.delta(
            "state",
            evidence="测试角色甲伤势稳定。",
        )
        item = StateRagDeltaV4ItemExtractionTests.valid_candidates()[2]
        advantage = self.valid_candidates()[6]
        assistant = (
            str(legacy["evidence"])
            + str(item["evidence"])
            + str(advantage["evidence"])
        )
        with tempfile.TemporaryDirectory() as temporary:
            config = StateRagExtractionCoverageTests.make_v3_config(
                Path(temporary)
            )
            normalized, skipped = state_runtime.validate_delta_v4_envelope(
                {
                    "schema_version": state_runtime.DELTA_V4_SCHEMA,
                    "deltas": [legacy, item, advantage],
                },
                assistant,
                config,
            )
        self.assertEqual([], skipped)
        legacy_values, item_values, advantage_values = (
            state_runtime.split_delta_v4_results_by_family(normalized)
        )
        self.assertEqual(["state"], [value["event_type"] for value in legacy_values])
        self.assertEqual(
            ["item_custody"],
            [value["event_type"] for value in item_values],
        )
        self.assertEqual(
            ["advantage_use"],
            [value["event_type"] for value in advantage_values],
        )
        with self.assertRaisesRegex(
            state_runtime.StateRagError,
            "ADVANTAGE_DELTA_V1_REQUIRES_STRICT_PROPOSAL_ADAPTER",
        ):
            state_runtime.split_delta_v4_results(normalized)

    def test_remote_computed_stable_and_control_fields_are_rejected(self) -> None:
        base = self.valid_candidates()[6]
        mutations: list[dict[str, Any]] = []
        computed = json.loads(json.dumps(base, ensure_ascii=False))
        computed["changes"]["effects"] = [
            {"before_state": {"charges": 2}}
        ]
        mutations.append(computed)
        stable = json.loads(json.dumps(base, ensure_ascii=False))
        stable["changes"]["effects"] = [
            {"advantage_id": "remote-id"}
        ]
        mutations.append(stable)
        control = json.loads(json.dumps(base, ensure_ascii=False))
        control["experience_contract_id"] = "remote-contract"
        mutations.append(control)
        for candidate in mutations:
            with self.subTest(candidate=candidate), self.assertRaises(
                state_runtime.StateRagError
            ):
                state_runtime.normalize_advantage_extraction_candidate(
                    candidate,
                    str(candidate["evidence"]),
                )

    def test_batch_injects_int_and_string_experience_bindings(self) -> None:
        candidates = self.valid_candidates()[6:8]
        assistant = "".join(str(value["evidence"]) for value in candidates)
        result = state_runtime.adapt_advantage_extraction_candidates(
            candidates,
            assistant,
            {
                "branch_id": "main",
                "chapter_no": 1,
                "scene_index": 0,
                "advantage_experience_required": True,
                "advantage_experience_bindings": {
                    0: {
                        "experience_contract_id": "experience-use",
                        "experience_contract_hash": "a" * 64,
                        "event_seed_id": "seed-use",
                        "event_seed_revision": 1,
                    },
                    "1": {
                        "experience_contract_id": "experience-reward",
                        "experience_contract_hash": "b" * 64,
                        "event_seed_id": "seed-reward",
                        "event_seed_revision": 2,
                    },
                },
            },
            self.resolver,
        )
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(
            ["experience-use", "experience-reward"],
            [
                event["experience_contract_id"]
                for event in result["events"]
            ],
        )
        self.assertEqual(
            ["seed-use", "seed-reward"],
            [
                event["causal_provenance"]["event_seed_id"]
                for event in result["events"]
            ],
        )

    def test_failed_creator_blocks_dependent_candidates(self) -> None:
        anchor, bind = self.valid_candidates()[1], self.valid_candidates()[3]

        def resolver(
            mention: str,
            reference_type: str,
            role: str,
        ) -> dict[str, Any]:
            if role == "anchor_ref":
                return {"status": "UNRESOLVED"}
            return self.resolver(mention, reference_type, role)

        result = state_runtime.adapt_advantage_extraction_candidates(
            [anchor, bind],
            str(anchor["evidence"]) + str(bind["evidence"]),
            {"branch_id": "main", "chapter_no": 1, "scene_index": 0},
            resolver,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(0, result["adapted_count"])
        self.assertEqual(
            [
                "ADVANTAGE_REFERENCE_UNRESOLVED",
                "ADVANTAGE_DEPENDENCY_UNRESOLVED",
            ],
            [issue["code"] for issue in result["issues"]],
        )
        self.assertEqual(
            0,
            result["issues"][1]["details"]["creator_candidate_index"],
        )

    def test_chat_prompt_contains_advantage_neutral_contract_and_example(
        self,
    ) -> None:
        candidate = self.valid_candidates()[6]
        assistant = str(candidate["evidence"])
        with tempfile.TemporaryDirectory() as temporary:
            config = StateRagExtractionCoverageTests.make_v3_config(
                Path(temporary)
            )
            with patch(
                "state_rag._remote_json",
                return_value=(
                    StateRagExtractionCoverageTests.response(
                        {
                            "schema_version": state_runtime.DELTA_V4_SCHEMA,
                            "deltas": [candidate],
                        }
                    ),
                    {"status": "ok"},
                ),
            ) as remote:
                deltas, skipped, _ = state_runtime._chat_extract(
                    config,
                    assistant,
                    "记录本轮金手指使用",
                    [],
                )
        self.assertEqual([], skipped)
        self.assertEqual(["advantage_use"], [value["event_type"] for value in deltas])
        system = remote.call_args.args[1]["messages"][0]["content"]
        self.assertIn(
            "BEGIN_VALID_ADVANTAGE_ENVELOPE_EXAMPLE",
            system,
        )
        self.assertIn("Never output plot-rag-advantage/v1", system)
        self.assertIn("experience_contract_id", system)
        example = json.loads(
            system.split(
                "BEGIN_VALID_ADVANTAGE_ENVELOPE_EXAMPLE ",
                1,
            )[1].split(
                " END_VALID_ADVANTAGE_ENVELOPE_EXAMPLE",
                1,
            )[0]
        )
        normalized, example_skipped = (
            state_runtime.validate_delta_v4_envelope(
                example,
                str(example["deltas"][0]["evidence"]),
                config,
            )
        )
        self.assertEqual([], example_skipped)
        self.assertEqual(
            ["advantage_use"],
            [value["event_type"] for value in normalized],
        )


if __name__ == "__main__":
    unittest.main()
