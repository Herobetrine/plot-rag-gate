from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from plot_init.inventory import extract_claims, inventory_sources  # noqa: E402
from plot_init import PlotInitService  # noqa: E402
from plot_init.remote_cache import (  # noqa: E402
    MemoryRemoteResponseCache,
    SQLiteRemoteResponseCache,
)
from plot_init.remote_model import load_remote_model_config  # noqa: E402


CLASSIFICATION_EVIDENCE = "这是一份边界模糊的资料。"
CLAIM_EVIDENCE = "叶舟把霜河城视为最后退路。"
TEST_KEY = "TOKEN_TEST_ONLY_PLOT_INIT_REMOTE"


class _InitRemoteHandler(BaseHTTPRequestHandler):
    classification_mode = "success"
    claim_mode = "success"
    calls = {"classification": 0, "claims": 0}
    authorization_headers: list[str] = []
    requests: list[dict[str, Any]] = []

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _write_raw(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return

    def _write_envelope(self, content: str) -> None:
        self._write_raw(
            json.dumps(
                {"choices": [{"message": {"content": content}}]},
                ensure_ascii=False,
            ).encode("utf-8")
        )

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(request)
        self.authorization_headers.append(
            str(self.headers.get("Authorization") or "")
        )
        messages = request.get("messages") or []
        user_content = messages[-1].get("content") if messages else "{}"
        user_payload = json.loads(str(user_content))
        task = str(user_payload.get("task") or "")
        type(self).calls[task] = type(self).calls.get(task, 0) + 1
        mode = (
            type(self).classification_mode
            if task == "classification"
            else type(self).claim_mode
        )

        if mode == "timeout":
            time.sleep(0.20)
        if mode == "http_429":
            self._write_raw(b"{}", status=429)
            return
        if mode == "invalid_envelope":
            self._write_raw(b"{not-json")
            return
        if mode == "empty_http":
            self._write_raw(b"")
            return
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/v1/chat/completions")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "empty_content":
            self._write_envelope("")
            return
        if mode == "invalid_content":
            self._write_envelope("{not-json")
            return

        if task == "classification":
            payload = {
                "source_role": "canon",
                "confidence": 0.99,
                "exact_evidence": CLASSIFICATION_EVIDENCE,
                # Hostile authority claims are accepted only as ignored input.
                "authority_tier": "T1",
                "ingest_policy": "include",
                "artifact_stage": "published",
                "scope_policy": "timeless",
                "canon_status": "accepted",
                "scope": "timeless",
                "field_status": "source_supported",
            }
            if mode == "bad_evidence":
                payload["exact_evidence"] = "来源中不存在的证据"
            self._write_envelope(json.dumps(payload, ensure_ascii=False))
            return

        claims: list[dict[str, Any]] = [
            {
                "subject": "叶舟",
                "predicate": "actor.goal",
                "object_or_value": "霜河城",
                "exact_evidence": CLAIM_EVIDENCE,
                "confidence": 0.98,
                # These fields can never cross the local proposal boundary.
                "authority_tier": "T1",
                "ingest_policy": "include",
                "canon_status": "accepted",
                "scope": "current",
                "field_status": "source_supported",
                "origin": "remote_canon",
            }
        ]
        if mode == "empty_claims":
            claims = []
        elif mode == "bad_evidence":
            claims[0]["exact_evidence"] = "叶舟已经抵达不存在之城。"
        self._write_envelope(
            json.dumps({"claims": claims}, ensure_ascii=False)
        )


class PlotInitRemoteReviewTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _InitRemoteHandler)
        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        _InitRemoteHandler.classification_mode = "success"
        _InitRemoteHandler.claim_mode = "success"
        _InitRemoteHandler.calls = {"classification": 0, "claims": 0}
        _InitRemoteHandler.authorization_headers = []
        _InitRemoteHandler.requests = []

    @contextmanager
    def remote_environment(
        self,
        *,
        enabled: bool = True,
        trusted: bool = True,
        timeout: str = "2",
    ) -> Iterator[None]:
        values = {
            "PLOT_RAG_INIT_REMOTE_ENABLED": "true" if enabled else "false",
            "PLOT_RAG_LLM_BASE_URL": self.base_url,
            "PLOT_RAG_LLM_MODEL": "mock-init-review-v1",
            "PLOT_RAG_LLM_API_KEY": TEST_KEY,
            "PLOT_RAG_LLM_TIMEOUT_SECONDS": timeout,
        }
        with patch.dict(os.environ, values, clear=False):
            if trusted:
                os.environ["PLOT_RAG_TRUSTED_HOSTS"] = "127.0.0.1"
            else:
                os.environ.pop("PLOT_RAG_TRUSTED_HOSTS", None)
            yield

    @staticmethod
    def document(text: str = CLAIM_EVIDENCE) -> dict[str, Any]:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return {
            "source_id": "src-test",
            "source_version_id": f"srcv-{digest[:16]}",
            "path": "source-1/ambiguous.txt",
            "real_path": "ambiguous.txt",
            "normalized_real_path": "ambiguous.txt",
            "content_hash": digest,
            "parse_status": "parsed",
            "ingest_policy": "review",
            "source_role": "note",
            "authority_tier": "T4",
            "artifact_stage": "brainstorm",
            "scope_policy": "preserve_unknown",
            "branch_id": "main",
            "classification_confidence": 0.45,
            "_text": text,
        }

    def test_low_confidence_classification_uses_cache_and_remains_review_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "ambiguous.txt"
            source.write_text(CLASSIFICATION_EVIDENCE, encoding="utf-8")
            cache = MemoryRemoteResponseCache()
            with self.remote_environment():
                first = inventory_sources([source], remote_cache=cache)
                second = inventory_sources([source], remote_cache=cache)

        first_doc = first["documents"][0]
        second_doc = second["documents"][0]
        self.assertEqual("canon", first_doc["source_role"])
        self.assertEqual("T4", first_doc["authority_tier"])
        self.assertEqual("review", first_doc["ingest_policy"])
        self.assertEqual("brainstorm", first_doc["artifact_stage"])
        self.assertEqual("preserve_unknown", first_doc["scope_policy"])
        self.assertEqual(
            "remote_ambiguity_proposal",
            first_doc["classification_basis"],
        )
        self.assertFalse(
            first_doc["remote_classification_review"]["cache_hit"]
        )
        self.assertRegex(
            first_doc["remote_classification_review"]["response_hash"],
            r"^[0-9a-f]{64}$",
        )
        self.assertTrue(
            second_doc["remote_classification_review"]["cache_hit"]
        )
        self.assertEqual(1, _InitRemoteHandler.calls["classification"])

    def test_claim_review_uses_cache_and_forces_model_proposal_fields(
        self,
    ) -> None:
        cache = MemoryRemoteResponseCache()
        document = self.document()
        with self.remote_environment():
            first = extract_claims(document, remote_cache=cache)
            second = extract_claims(document, remote_cache=cache)

        self.assertEqual(1, len(first))
        claim = first[0]
        self.assertEqual("actor.goal", claim["predicate"])
        self.assertEqual("T4", claim["authority_tier"])
        self.assertEqual("model_proposed", claim["field_status"])
        self.assertEqual("proposed", claim["canon_status"])
        self.assertEqual("remote_ambiguity_proposal", claim["origin"])
        self.assertIsNone(claim["scope"])
        self.assertEqual(CLAIM_EVIDENCE, claim["exact_evidence"])
        self.assertFalse(claim["remote_review"]["cache_hit"])
        self.assertTrue(second[0]["remote_review"]["cache_hit"])
        self.assertEqual(1, _InitRemoteHandler.calls["claims"])
        request = _InitRemoteHandler.requests[0]
        self.assertIn("predicate 必须是英文稳定标识", request["messages"][0]["content"])
        user_payload = json.loads(request["messages"][-1]["content"])
        claim_schema = user_payload["output_schema"]["properties"]["claims"]["items"]
        self.assertEqual(
            {
                "subject",
                "predicate",
                "object_or_value",
                "exact_evidence",
                "confidence",
            },
            set(claim_schema["properties"]),
        )
        self.assertIn(
            "world",
            claim_schema["properties"]["predicate"]["pattern"],
        )

    def test_disabled_and_untrusted_hosts_make_zero_network_calls(self) -> None:
        with self.remote_environment(enabled=False):
            disabled_doc = self.document()
            disabled = extract_claims(
                disabled_doc,
                remote_cache=MemoryRemoteResponseCache(),
            )
        self.assertEqual([], disabled)
        self.assertEqual(
            "REMOTE_DISABLED",
            disabled_doc["remote_claim_review"]["error_code"],
        )
        self.assertEqual(0, _InitRemoteHandler.calls["claims"])

    def test_shared_siliconflow_key_cannot_egress_to_custom_trusted_host(
        self,
    ) -> None:
        values = {
            "PLOT_RAG_INIT_REMOTE_ENABLED": "true",
            "PLOT_RAG_LLM_BASE_URL": self.base_url,
            "PLOT_RAG_LLM_MODEL": "mock-init-review-v1",
            "PLOT_RAG_LLM_TIMEOUT_SECONDS": "2",
            "PLOT_RAG_TRUSTED_HOSTS": "127.0.0.1",
            "SILICONFLOW_API_KEY": TEST_KEY,
        }
        with patch.dict(os.environ, values, clear=True):
            config = load_remote_model_config()
            document = self.document()
            claims = extract_claims(
                document,
                remote_cache=MemoryRemoteResponseCache(),
            )

        self.assertFalse(config.ready)
        self.assertEqual(
            "REMOTE_CREDENTIAL_HOST_MISMATCH",
            config.error_code,
        )
        self.assertEqual([], claims)
        self.assertEqual(
            "REMOTE_CREDENTIAL_HOST_MISMATCH",
            document["remote_claim_review"]["error_code"],
        )
        self.assertEqual(0, _InitRemoteHandler.calls["claims"])

        with self.remote_environment(trusted=False):
            untrusted_doc = self.document()
            untrusted = extract_claims(
                untrusted_doc,
                remote_cache=MemoryRemoteResponseCache(),
            )
        self.assertEqual([], untrusted)
        self.assertEqual(
            "REMOTE_HOST_UNTRUSTED",
            untrusted_doc["remote_claim_review"]["error_code"],
        )
        self.assertEqual(0, _InitRemoteHandler.calls["claims"])

    def test_confident_local_results_do_not_call_remote_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            chapter_dir = Path(temp_dir) / "正文"
            chapter_dir.mkdir()
            source = chapter_dir / "第一章.md"
            source.write_text(
                "状态：已发布\n# 叶舟\n当前位置：霜河城\n",
                encoding="utf-8",
            )
            with self.remote_environment():
                result = inventory_sources(
                    [source],
                    remote_cache=MemoryRemoteResponseCache(),
                )
                claims = extract_claims(
                    result["documents"][0],
                    remote_cache=MemoryRemoteResponseCache(),
                )
        self.assertGreaterEqual(len(claims), 1)
        self.assertEqual("T1", result["documents"][0]["authority_tier"])
        self.assertEqual(0, _InitRemoteHandler.calls["classification"])
        self.assertEqual(0, _InitRemoteHandler.calls["claims"])

    def test_invalid_or_failed_responses_are_not_cached(self) -> None:
        cases = {
            "timeout": "REMOTE_TIMEOUT",
            "http_429": "REMOTE_HTTP_429",
            "invalid_envelope": "REMOTE_RESPONSE_INVALID_JSON",
            "empty_http": "REMOTE_RESPONSE_EMPTY",
            "empty_content": "REMOTE_CONTENT_EMPTY",
            "invalid_content": "REMOTE_CONTENT_INVALID_JSON",
            "empty_claims": "REMOTE_CLAIMS_EMPTY",
            "bad_evidence": "REMOTE_EVIDENCE_INVALID",
            "redirect": "REMOTE_REDIRECT_BLOCKED",
        }
        for mode, expected_code in cases.items():
            with self.subTest(mode=mode):
                _InitRemoteHandler.claim_mode = mode
                _InitRemoteHandler.calls["claims"] = 0
                cache = MemoryRemoteResponseCache()
                timeout = "0.05" if mode == "timeout" else "2"
                with self.remote_environment(timeout=timeout):
                    first_doc = self.document()
                    second_doc = self.document()
                    self.assertEqual(
                        [],
                        extract_claims(first_doc, remote_cache=cache),
                    )
                    self.assertEqual(
                        [],
                        extract_claims(second_doc, remote_cache=cache),
                    )
                self.assertEqual(2, _InitRemoteHandler.calls["claims"])
                self.assertEqual(
                    expected_code,
                    second_doc["remote_claim_review"]["error_code"],
                )

    def test_sqlite_cache_and_diagnostics_never_persist_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "init.sqlite3"
            cache = SQLiteRemoteResponseCache(database_path)
            document = self.document()
            with self.remote_environment():
                claims = extract_claims(document, remote_cache=cache)
            self.assertEqual(1, len(claims))
            self.assertTrue(database_path.is_file())
            self.assertNotIn(TEST_KEY.encode("utf-8"), database_path.read_bytes())
            with closing(sqlite3.connect(database_path)) as connection:
                response_json = str(
                    connection.execute(
                        "SELECT response_json "
                        "FROM initialization_remote_response_cache"
                    ).fetchone()[0]
                )
            self.assertNotIn(TEST_KEY, response_json)
            self.assertEqual(
                [f"Bearer {TEST_KEY}"],
                _InitRemoteHandler.authorization_headers,
            )
            self.assertNotIn(
                TEST_KEY,
                json.dumps(
                    {
                        "document": document,
                        "claims": claims,
                        "cache": cache.describe(),
                    },
                    ensure_ascii=False,
                ),
            )

    def test_bundle_provenance_reports_remote_reviews_without_secrets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            source = workspace / "ambiguous.txt"
            source.write_text(
                f"{CLASSIFICATION_EVIDENCE}\n{CLAIM_EVIDENCE}\n",
                encoding="utf-8",
            )
            with self.remote_environment():
                result = PlotInitService(workspace).dry_run(
                    project_root=workspace / "novel",
                    mode="ingest",
                    sources=[source],
                )

        provenance = result["bundle"]["provenance"]
        self.assertTrue(provenance["remote_model_used"])
        self.assertEqual(
            "local-deterministic-v1+remote-ambiguity-review-v1",
            provenance["extractor"],
        )
        summary = provenance["remote_review"]
        self.assertEqual(2, summary["review_count"])
        self.assertEqual(2, summary["accepted_count"])
        self.assertEqual(["mock-init-review-v1"], summary["models"])
        self.assertEqual(2, len(summary["response_hashes"]))
        self.assertEqual(
            {"classification", "claims"},
            {review["stage"] for review in summary["reviews"]},
        )
        self.assertIn(
            "remote_claim_review",
            result["source_manifest"][0],
        )
        self.assertNotIn(
            TEST_KEY,
            json.dumps(result, ensure_ascii=False),
        )

    def test_local_bundle_provenance_is_stable_when_remote_is_disabled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            source = workspace / "ambiguous.txt"
            source.write_text(
                f"{CLASSIFICATION_EVIDENCE}\n{CLAIM_EVIDENCE}\n",
                encoding="utf-8",
            )
            service = PlotInitService(workspace)
            with self.remote_environment(enabled=False):
                first = service.dry_run(
                    project_root=workspace / "novel",
                    mode="ingest",
                    sources=[source],
                )
                second = service.dry_run(
                    project_root=workspace / "novel",
                    mode="ingest",
                    sources=[source],
                )

        first_bundle = first["bundle"]
        second_bundle = second["bundle"]
        first_provenance = first_bundle["provenance"]
        second_provenance = second_bundle["provenance"]
        self.assertFalse(first_provenance["remote_model_used"])
        self.assertEqual(
            "local-deterministic-v1",
            first_provenance["extractor"],
        )
        self.assertEqual(
            first_bundle["bundle_hash"],
            second_bundle["bundle_hash"],
        )
        self.assertEqual(first_provenance, second_provenance)
        self.assertEqual(0, _InitRemoteHandler.calls["classification"])
        self.assertEqual(0, _InitRemoteHandler.calls["claims"])


if __name__ == "__main__":
    unittest.main()
