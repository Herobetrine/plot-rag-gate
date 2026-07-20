from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from longform import (  # noqa: E402
    AUTHORITY_INDEX_SCHEMA_VERSION,
    PROJECTION_NAMES,
    AcceptedSummaryStore,
    AuthorityIndex,
    AuthoritySource,
    ContextContractBuilder,
    LayeredMemoryStore,
    ProjectPatternStore,
    ProjectionJournal,
    WebnovelMethodPack,
    decompose_continuity_needs,
    run_annotation_benchmark,
    stable_normalized_hash,
    validate_annotation_manifest,
)
from longform.projections import ProjectionRunError  # noqa: E402
import longform.authority as authority_module  # noqa: E402
from longform.benchmarking import (  # noqa: E402
    evaluate_annotation_record,
    extract_proposal_candidates,
)
import v1_runtime  # noqa: E402


class _LongformProviderHandler(BaseHTTPRequestHandler):
    embedding_requests: list[dict[str, object]] = []
    rerank_requests: list[dict[str, object]] = []
    authorization_headers: list[str] = []

    @staticmethod
    def _vector(text: str) -> list[float]:
        if "火焰巨兽" in text or "喷火" in text:
            return [0.0, 1.0]
        return [1.0, 0.0]

    def _write_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.authorization_headers.append(
            str(self.headers.get("Authorization") or "")
        )
        if self.path.endswith("/embeddings"):
            self.embedding_requests.append(payload)
            inputs = payload.get("input") or []
            self._write_json(
                {
                    "data": [
                        {
                            "index": index,
                            "embedding": self._vector(str(text)),
                        }
                        for index, text in enumerate(inputs)
                    ]
                }
            )
            return
        if self.path.endswith("/rerank"):
            self.rerank_requests.append(payload)
            documents = [str(value) for value in payload.get("documents") or []]
            results = [
                {
                    "index": index,
                    "relevance_score": (
                        0.99 if "苹果园" in document else 0.10
                    ),
                }
                for index, document in enumerate(documents)
            ]
            self._write_json({"results": results})
            return
        self.send_error(404)

    def log_message(self, *_args: object) -> None:
        return


class LongformEngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_project(self) -> Path:
        project = self.base / "长篇项目"
        (project / "正文").mkdir(parents=True)
        (project / "设定集").mkdir(parents=True)
        (project / "笔记").mkdir(parents=True)
        return project

    @staticmethod
    def sources() -> list[AuthoritySource]:
        return [
            AuthoritySource(
                glob="正文/**/*.md",
                role="canon",
                priority=100,
                scope_policy="infer_and_review",
                ingest_policy="include",
            ),
            AuthoritySource(
                glob="设定集/**/*.md",
                role="setting",
                priority=80,
                scope_policy="timeless",
                ingest_policy="include",
            ),
            AuthoritySource(
                glob="笔记/**/*.md",
                role="note",
                priority=5,
                scope_policy="planned",
                ingest_policy="review",
            ),
            AuthoritySource(
                glob="笔记/排除*.md",
                role="note",
                priority=999,
                scope_policy="planned",
                ingest_policy="exclude",
            ),
        ]

    def test_authority_index_has_independent_schema_roles_and_fallback(self) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "唯一标记 ALPHA77：测试角色甲当前位于测试城东门。",
            encoding="utf-8",
        )
        (project / "设定集" / "城市.md").write_text(
            "测试城使用列车完成跨层通行。",
            encoding="utf-8",
        )
        (project / "笔记" / "灵感.md").write_text(
            "ALPHA77 也许以后改成城外，尚未验收。",
            encoding="utf-8",
        )
        (project / "笔记" / "排除草稿.md").write_text(
            "ALPHA77 rejected draft claims the moon.",
            encoding="utf-8",
        )
        index = AuthorityIndex(
            self.base / "authority.sqlite3",
            force_lexical_fallback=True,
        )
        stats = index.refresh(project, self.sources())
        info = index.schema_info()
        self.assertEqual(AUTHORITY_INDEX_SCHEMA_VERSION, info["authority_index_schema_version"])
        self.assertFalse(info["fts5_available"])
        self.assertEqual(3, stats["files_parsed"])
        results = index.search("ALPHA77 测试角色甲 当前 位于", limit=4)
        self.assertTrue(results)
        self.assertEqual("canon", results[0]["role"])
        self.assertEqual("正文/第一章.md", results[0]["path"])
        self.assertTrue(
            all(item["path"] != "笔记/排除草稿.md" for item in results)
        )
        self.assertEqual("lexical_fallback", results[0]["retrieval_mode"])

    def test_authority_index_hard_excludes_runtime_git_and_external_links(self) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "VISIBLE_CANON_MARKER 只允许这一份进入权威索引。",
            encoding="utf-8",
        )
        for reserved in (".git", ".plot-rag", ".plot-rag-init"):
            path = project / reserved
            path.mkdir(parents=True, exist_ok=True)
            (path / "hidden.md").write_text(
                f"HIDDEN_{reserved}_MARKER",
                encoding="utf-8",
            )
        outside = self.base / "outside.md"
        outside.write_text("EXTERNAL_LINK_MARKER", encoding="utf-8")
        link = project / "正文" / "external-link.md"
        try:
            link.symlink_to(outside)
            link_created = True
        except OSError:
            link_created = False

        index = AuthorityIndex(self.base / "hard-exclude.sqlite3")
        stats = index.refresh(
            project,
            [
                AuthoritySource(
                    glob="**/*.md",
                    role="canon",
                    priority=100,
                    scope_policy="current",
                    ingest_policy="include",
                )
            ],
        )

        self.assertEqual(1, stats["files_parsed"])
        self.assertEqual(
            "正文/第一章.md",
            index.search("VISIBLE_CANON_MARKER", limit=3)[0]["path"],
        )
        self.assertEqual([], index.search("HIDDEN_MARKER", limit=10))
        if link_created:
            self.assertEqual([], index.search("EXTERNAL_LINK_MARKER", limit=10))

    def test_sha256_incremental_refresh_skips_parse_chunk_and_embedding(self) -> None:
        project = self.make_project()
        chapter = project / "正文" / "第一章.md"
        chapter.write_text("HASHMARK1 测试角色甲持有青铜钥匙。", encoding="utf-8")
        calls: list[str] = []

        def embed(text: str) -> list[float]:
            calls.append(text)
            return [float(len(text)), 1.0]

        index = AuthorityIndex(
            self.base / "incremental.sqlite3",
            embedding_provider=embed,
            embedding_model="fixture-vector-v1",
        )
        first = index.refresh(project, self.sources())
        self.assertEqual(1, first["files_parsed"])
        self.assertGreater(first["embedding_calls"], 0)
        first_call_count = len(calls)

        old = chapter.stat()
        os.utime(chapter, ns=(old.st_atime_ns, old.st_mtime_ns + 5_000_000))
        second = index.refresh(project, self.sources())
        self.assertEqual(1, second["files_hashed"])
        self.assertGreater(second["bytes_hashed"], 0)
        self.assertEqual(1, second["files_unchanged"])
        self.assertEqual(0, second["files_parsed"])
        self.assertEqual(0, second["chunks_written"])
        self.assertEqual(0, second["embedding_calls"])
        self.assertEqual(first_call_count, len(calls))

        policy_sources = self.sources()
        policy_sources[0] = AuthoritySource(
            glob="正文/**/*.md",
            role="canon",
            priority=220,
            scope_policy="current",
            ingest_policy="include",
        )
        policy_only = index.refresh(project, policy_sources)
        self.assertEqual(0, policy_only["files_parsed"])
        self.assertEqual(1, policy_only["source_policies_updated"])
        self.assertEqual(0, policy_only["embedding_calls"])
        self.assertEqual(220, index.search("HASHMARK1 青铜钥匙")[0]["priority"])

        changed_stat = chapter.stat()
        chapter.write_text("HASHMARK2 测试角色甲已经消耗青铜钥匙。", encoding="utf-8")
        os.utime(
            chapter,
            ns=(changed_stat.st_atime_ns, changed_stat.st_mtime_ns),
        )
        third = index.refresh(project, policy_sources)
        self.assertEqual(1, third["files_parsed"])
        self.assertGreater(third["embedding_calls"], 0)
        self.assertGreater(len(calls), first_call_count)

    def test_fts_or_fallback_persists_bm25_candidates_and_cache(self) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "CACHEMARK88 测试角色甲必须在午夜前兑现列车承诺。",
            encoding="utf-8",
        )
        index = AuthorityIndex(self.base / "fts.sqlite3")
        index.refresh(project, self.sources())
        first = index.search("CACHEMARK88 午夜 列车 承诺", limit=3)
        second = index.search("CACHEMARK88 午夜 列车 承诺", limit=3)
        self.assertTrue(first)
        self.assertFalse(first[0]["candidate_cache_hit"])
        self.assertTrue(second[0]["candidate_cache_hit"])
        expected_mode = (
            "fts5_bm25" if index.schema_info()["fts5_available"] else "lexical_fallback"
        )
        self.assertEqual(expected_mode, first[0]["retrieval_mode"])

    def test_legacy_single_embedding_reuses_exact_cache(self) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "LEGACY_EMBED_CACHE 测试角色甲仍在测试城。",
            encoding="utf-8",
        )
        calls: list[str] = []

        def embed(text: str) -> list[float]:
            calls.append(text)
            return [1.0, 0.0]

        index = AuthorityIndex(
            self.base / "legacy-query-cache.sqlite3",
            embedding_provider=embed,
            embedding_model="fixture-legacy-query-cache-v1",
            query_embedding_cache_size=8,
            singleflight_enabled=False,
        )
        index.refresh(project, self.sources())
        before_queries = len(calls)
        first = index.search(
            "LEGACY_EMBED_CACHE 测试城",
            limit=1,
            use_candidate_cache=False,
        )
        after_first = len(calls)
        second = index.search(
            "LEGACY_EMBED_CACHE 测试城",
            limit=1,
            use_candidate_cache=False,
        )
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(before_queries + 1, after_first)
        self.assertEqual(after_first, len(calls))
        self.assertEqual("ok", second[0]["embedding_status"])

    def test_candidate_cache_is_invalidated_when_rerank_order_changes(
        self,
    ) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "CACHE_VERSION_MARKER 测试角色甲在测试城。",
            encoding="utf-8",
        )
        calls = 0

        def rerank(
            _query: str,
            documents: list[str],
            _top_n: int,
        ) -> list[tuple[int, float]]:
            nonlocal calls
            calls += 1
            return [
                (index, float(len(documents) - index))
                for index in range(len(documents))
            ]

        database = self.base / "candidate-cache-version.sqlite3"
        with patch.object(
            authority_module,
            "_AUTHORITY_SCORING_VERSION",
            "authority-hybrid-score/v2",
        ):
            old_index = AuthorityIndex(
                database,
                rerank_provider=rerank,
                rerank_model="fixture-cache-version-v1",
            )
            old_index.refresh(project, self.sources())
            old_result = old_index.search(
                "CACHE_VERSION_MARKER 测试城",
                limit=1,
                use_candidate_cache=True,
            )
            self.assertTrue(old_result)
            self.assertFalse(old_result[0]["candidate_cache_hit"])

        with patch.object(
            authority_module,
            "_AUTHORITY_SCORING_VERSION",
            authority_module._AUTHORITY_SCORING_VERSION,
        ):
            new_index = AuthorityIndex(
                database,
                rerank_provider=rerank,
                rerank_model="fixture-cache-version-v1",
            )
            new_result = new_index.search(
                "CACHE_VERSION_MARKER 测试城",
                limit=1,
                use_candidate_cache=True,
            )
            self.assertTrue(new_result)
            self.assertFalse(
                new_result[0]["candidate_cache_hit"],
                "new scoring version must miss old candidate cache",
            )
        self.assertGreaterEqual(calls, 1)

    def test_vectors_change_recall_and_rerank_provider_reorders_candidates(
        self,
    ) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "苹果园里保存着春季灌溉账册。",
            encoding="utf-8",
        )
        (project / "正文" / "第二章.md").write_text(
            "火焰巨兽栖息在赤红山脉深处。",
            encoding="utf-8",
        )
        embedding_calls: list[str] = []

        def embed(text: str) -> list[float]:
            embedding_calls.append(text)
            if "火焰巨兽" in text or "喷火" in text:
                return [0.0, 1.0]
            return [1.0, 0.0]

        database = self.base / "hybrid-vector.sqlite3"
        vector_index = AuthorityIndex(
            database,
            embedding_provider=embed,
            embedding_model="mock-embedding-v1",
        )
        refresh = vector_index.refresh(project, self.sources())
        self.assertEqual(2, refresh["embedding_calls"])

        lexical_only = AuthorityIndex(database)
        query = "那只会喷火的危险生物在哪里"
        self.assertEqual([], lexical_only.search(query, limit=2))

        semantic = vector_index.search(query, limit=2)
        self.assertEqual("正文/第二章.md", semantic[0]["path"])
        self.assertIn(
            semantic[0]["retrieval_mode"],
            {"vector", "hybrid_vector_bm25", "hybrid_vector_lexical"},
        )
        self.assertEqual("ok", semantic[0]["embedding_status"])
        self.assertIsNotNone(semantic[0]["vector_score"])
        cached_semantic = vector_index.search(query, limit=2)
        self.assertTrue(cached_semantic[0]["candidate_cache_hit"])

        rerank_calls: list[tuple[str, list[str], int]] = []

        def rerank(
            rerank_query: str,
            documents: list[str],
            top_n: int,
        ) -> list[tuple[int, float]]:
            rerank_calls.append((rerank_query, list(documents), top_n))
            return [
                (
                    index,
                    0.99 if "苹果园" in document else 0.10,
                )
                for index, document in enumerate(documents)
            ]

        reranked_index = AuthorityIndex(
            database,
            embedding_provider=embed,
            embedding_model="mock-embedding-v1",
            rerank_provider=rerank,
            rerank_model="mock-rerank-v1",
        )
        reranked = reranked_index.search(query, limit=2)
        self.assertEqual(1, len(rerank_calls))
        self.assertEqual("正文/第一章.md", reranked[0]["path"])
        self.assertEqual("ok", reranked[0]["rerank_status"])
        self.assertEqual(0, reranked[0]["rerank_rank"])
        self.assertTrue(reranked[0]["retrieval_mode"].startswith("reranked_"))

        before_new_model = len(embedding_calls)
        new_model_index = AuthorityIndex(
            database,
            embedding_provider=embed,
            embedding_model="mock-embedding-v2",
        )
        new_model_refresh = new_model_index.refresh(project, self.sources())
        self.assertEqual(2, new_model_refresh["embedding_calls"])
        repeated = new_model_index.refresh(project, self.sources())
        self.assertEqual(0, repeated["embedding_calls"])
        self.assertEqual(
            before_new_model + 2,
            len(embedding_calls),
        )

    def test_remote_embedding_and_rerank_failures_keep_lexical_results(
        self,
    ) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "FALLBACK_MARKER 测试角色甲仍在测试城站台。",
            encoding="utf-8",
        )
        fail_query = False
        fail_rerank = True

        def embed(text: str) -> list[float]:
            if fail_query:
                raise TimeoutError("mock embedding timeout")
            return [1.0, 0.0]

        def rerank(
            _query: str,
            documents: list[str],
            _top_n: int,
        ) -> list[tuple[int, float]]:
            if fail_rerank:
                raise TimeoutError("mock rerank timeout")
            return [(index, 1.0) for index, _ in enumerate(documents)]

        index = AuthorityIndex(
            self.base / "hybrid-fallback.sqlite3",
            embedding_provider=embed,
            embedding_model="mock-embedding-v1",
            rerank_provider=rerank,
            rerank_model="mock-rerank-v1",
        )
        index.refresh(project, self.sources())
        fail_query = True
        results = index.search(
            "FALLBACK_MARKER 测试城",
            limit=2,
        )
        self.assertTrue(results)
        self.assertEqual("正文/第一章.md", results[0]["path"])
        self.assertEqual("failed", results[0]["embedding_status"])
        self.assertEqual("failed", results[0]["rerank_status"])
        self.assertIn(
            results[0]["retrieval_mode"],
            {"fts5_bm25", "lexical_fallback"},
        )
        fail_query = False
        fail_rerank = False
        recovered = index.search("FALLBACK_MARKER 测试城", limit=2)
        self.assertEqual("ok", recovered[0]["embedding_status"])
        self.assertEqual("ok", recovered[0]["rerank_status"])
        self.assertFalse(recovered[0]["candidate_cache_hit"])
        cached = index.search("FALLBACK_MARKER 测试城", limit=2)
        self.assertTrue(cached[0]["candidate_cache_hit"])

    def test_v1_runtime_uses_state_rag_mock_siliconflow_wrappers(self) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "苹果园里保存着春季灌溉账册。",
            encoding="utf-8",
        )
        (project / "正文" / "第二章.md").write_text(
            "火焰巨兽栖息在赤红山脉深处。",
            encoding="utf-8",
        )
        (project / ".plot-rag").mkdir(exist_ok=True)
        config = {
            "config_version": 3,
            "enabled": True,
            "lifecycle": {
                "strict": True,
                "index_embeddings_on_prepare": True,
            },
            "performance": {
                "prepare_v2": {
                    "batch_embedding": True,
                    "embedding_batch_size": 3,
                    "embedding_batch_max_chars": 4096,
                    "rerank_max_concurrency": 2,
                    "singleflight": True,
                }
            },
            "authority_sources": [
                {
                    "glob": "正文/**/*.md",
                    "role": "canon",
                    "priority": 100,
                    "scope_policy": "infer_and_review",
                    "ingest_policy": "include",
                }
            ],
            "remote": {
                "timeout_seconds": 3,
                "embedding": {
                    "enabled": True,
                    "model": "mock-embedding-v1",
                    "api_key_env": "PLOT_RAG_EMBED_API_KEY",
                },
                "rerank": {
                    "enabled": True,
                    "model": "mock-rerank-v1",
                    "api_key_env": "PLOT_RAG_RERANK_API_KEY",
                },
                "extract": {"enabled": False},
            },
        }
        config_path = project / ".plot-rag" / "config.json"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )

        _LongformProviderHandler.embedding_requests = []
        _LongformProviderHandler.rerank_requests = []
        _LongformProviderHandler.authorization_headers = []
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _LongformProviderHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}/v1"
        test_key = "TEST_ONLY_SILICONFLOW_KEY"
        try:
            with patch.dict(
                os.environ,
                {
                    "PLOT_RAG_EMBED_API_KEY": test_key,
                    "PLOT_RAG_RERANK_API_KEY": test_key,
                    "EMBED_BASE_URL": base_url,
                    "RERANK_BASE_URL": base_url,
                    "PLOT_RAG_TRUSTED_HOSTS": "127.0.0.1",
                },
                clear=False,
            ):
                refreshed = v1_runtime.refresh_longform_index(
                    project,
                    with_embeddings=True,
                )
                self.assertEqual(
                    2,
                    refreshed["refresh"]["embedding_calls"],
                )
                index = v1_runtime._authority_index(
                    project,
                    with_embeddings=True,
                    with_rerank=True,
                )
                self.assertIsNotNone(index.embedding_batch_provider)
                self.assertEqual(3, index.embedding_batch_size)
                self.assertEqual(4096, index.embedding_batch_max_chars)
                self.assertEqual(2, index.rerank_max_concurrency)
                results = index.search(
                    "那只会喷火的危险生物在哪里",
                    limit=2,
                    use_candidate_cache=False,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertGreaterEqual(
            len(_LongformProviderHandler.embedding_requests),
            3,
        )
        self.assertEqual(
            1,
            len(_LongformProviderHandler.rerank_requests),
        )
        self.assertEqual("正文/第一章.md", results[0]["path"])
        self.assertEqual("ok", results[0]["embedding_status"])
        self.assertEqual("ok", results[0]["rerank_status"])
        self.assertTrue(
            all(
                header == f"Bearer {test_key}"
                for header in _LongformProviderHandler.authorization_headers
            )
        )
        self.assertNotIn(test_key, config_path.read_text(encoding="utf-8"))
        self.assertNotIn(
            test_key.encode("utf-8"),
            v1_runtime._authority_index_path(project).read_bytes(),
        )

    def test_siliconflow_bge_m3_uses_singleton_exact_embeddings(self) -> None:
        project = self.make_project()
        service = v1_runtime.state_rag.ServiceConfig(
            name="embedding",
            enabled=True,
            base_url="https://api.siliconflow.cn/v1",
            model="BAAI/bge-m3",
            api_key_env="SILICONFLOW_API_KEY",
            api_key_required=True,
            endpoint="embeddings",
            timeout_seconds=30.0,
        )
        other_model = v1_runtime.state_rag.ServiceConfig(
            name="embedding",
            enabled=True,
            base_url="https://api.siliconflow.cn/v1",
            model="fixture-model",
            api_key_env="FIXTURE_API_KEY",
            api_key_required=False,
            endpoint="embeddings",
            timeout_seconds=30.0,
        )
        trailing_dot = v1_runtime.state_rag.ServiceConfig(
            name="embedding",
            enabled=True,
            base_url="https://api.siliconflow.cn./v1",
            model="BAAI/bge-m3",
            api_key_env="SILICONFLOW_API_KEY",
            api_key_required=True,
            endpoint="embeddings",
            timeout_seconds=30.0,
        )
        self.assertFalse(v1_runtime._embedding_batch_is_exact(service))
        self.assertFalse(v1_runtime._embedding_batch_is_exact(trailing_dot))
        self.assertTrue(v1_runtime._embedding_batch_is_exact(other_model))

        runtime = v1_runtime.SimpleNamespace(embedding=service)
        with patch.object(
            v1_runtime,
            "load_config",
            return_value={
                "performance": {
                    "prepare_v2": {
                        "batch_embedding": True,
                    }
                }
            },
        ), patch.object(
            v1_runtime.state_rag,
            "_load_runtime_config",
            return_value=runtime,
        ):
            index = v1_runtime._authority_index(
                project,
                with_embeddings=True,
                with_rerank=False,
                prepare_v2_enabled=True,
            )

        self.assertIsNone(index.embedding_batch_provider)
        self.assertEqual(4, index.embedding_single_max_concurrency)
        identity = json.loads(
            str(getattr(index.embedding_provider, "cache_identity"))
        )
        self.assertEqual("singleton_exact", identity["input_semantics"])

        rerank_service = v1_runtime.state_rag.ServiceConfig(
            name="rerank",
            enabled=True,
            base_url="https://api.siliconflow.cn/v1",
            model="BAAI/bge-reranker-v2-m3",
            api_key_env="SILICONFLOW_API_KEY",
            api_key_required=True,
            endpoint="rerank",
            timeout_seconds=30.0,
        )
        bounded_runtime = v1_runtime.SimpleNamespace(
            embedding=service,
            rerank=rerank_service,
        )
        with patch.object(
            v1_runtime,
            "load_config",
            return_value={
                "performance": {
                    "prepare_v2": {
                        "batch_embedding": True,
                        "remote_total_concurrency": 1,
                        "rerank_max_concurrency": 4,
                    }
                }
            },
        ), patch.object(
            v1_runtime.state_rag,
            "_load_runtime_config",
            return_value=bounded_runtime,
        ):
            bounded = v1_runtime._authority_index(
                project,
                with_embeddings=True,
                with_rerank=True,
                prepare_v2_enabled=True,
            )
        self.assertEqual(1, bounded.embedding_single_max_concurrency)
        self.assertEqual(1, bounded.rerank_max_concurrency)

    def test_atomic_needs_and_mandatory_context_stay_inside_budget(self) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "测试角色甲正在测试城站台等待列车，青铜钥匙仍在袖中。",
            encoding="utf-8",
        )
        index = AuthorityIndex(self.base / "contract-index.sqlite3")
        index.refresh(project, self.sources())
        memory = LayeredMemoryStore(self.base / "memory.sqlite3")
        commit = {
            "commit_id": "commit-001",
            "canon_status": "accepted",
            "artifact_stage": "final",
            "branch_id": "main",
            "chapter_no": 1,
            "arc_id": "arc-1",
            "volume_id": "volume-1",
            "text": "测试角色甲在站台等车。",
            "summary": "测试角色甲抵达站台。",
            "current_state": ["测试角色甲受轻伤，当前目标是登上列车。"],
            "open_loops": ["青铜钥匙必须在午夜前交给接头人。"],
            "events": ["测试角色甲抵达测试城站台。"],
            "semantic_facts": ["跨层通行只能使用列车基础设施。"],
        }
        memory.project_accepted_commit(commit)
        prompt = "继续写测试角色甲进入列车，核对他的位置、道具、与接头人的关系和午夜期限，并处理青铜钥匙伏笔。"
        needs = decompose_continuity_needs(prompt)
        self.assertGreaterEqual(len(needs), 1)
        self.assertLessEqual(len(needs), 5)
        self.assertEqual("current_state", needs[0].category)
        builder = ContextContractBuilder(index, memory_store=memory)
        contract = builder.build(prompt, task="prose", max_context_chars=520)
        self.assertTrue(contract["within_budget"])
        self.assertLessEqual(contract["context_chars"], 520)
        self.assertEqual([], contract["missing_mandatory"])
        self.assertGreaterEqual(len(contract["sections"]["current_state"]), 1)
        self.assertGreaterEqual(len(contract["sections"]["open_loop"]), 1)

    def test_context_builder_batches_all_atomic_needs_once_in_stable_order(
        self,
    ) -> None:
        prompt = (
            "继续写测试角色甲前往测试城，核对他的位置、道具、关系和午夜期限，"
            "并处理青铜钥匙伏笔。"
        )
        expected_needs = decompose_continuity_needs(prompt)

        class RecordingIndex:
            def __init__(self) -> None:
                self.calls: list[list[dict[str, object]]] = []

            def search_many(
                self,
                requests: object,
            ) -> list[list[dict[str, object]]]:
                normalized = [dict(item) for item in requests]
                self.calls.append(normalized)
                return [
                    [
                        {
                            "chunk_id": f"chunk-{index}",
                            "content_sha256": f"sha-{index}",
                            "path": f"正文/第{index + 1}章.md",
                            "ordinal": index,
                            "text": f"need-{index} accepted evidence",
                            "role": "canon",
                            "scope_policy": "infer_and_review",
                            "ingest_policy": "include",
                            "priority": 100,
                            "score": 1.0,
                            "retrieval_mode": "fts5_bm25",
                        }
                    ]
                    for index in range(len(normalized))
                ]

            def search(self, *_args: object, **_kwargs: object) -> object:
                raise AssertionError("builder must use one search_many call")

            @staticmethod
            def last_search_diagnostics() -> dict[str, object]:
                return {"query_count": len(expected_needs)}

        index = RecordingIndex()
        contract = ContextContractBuilder(index).build(
            prompt,
            task="outline",
            authority_limit=7,
            max_context_chars=4000,
            category_quotas={
                "accepted_authority": 1,
                "current_state": 0,
                "open_loop": 0,
            },
        )

        self.assertEqual(1, len(index.calls))
        requests = index.calls[0]
        self.assertEqual(len(expected_needs), len(requests))
        self.assertEqual(
            list(range(len(expected_needs))),
            [int(item["need_index"]) for item in requests],
        )
        self.assertEqual(
            [need.query for need in expected_needs],
            [str(item["query"]) for item in requests],
        )
        self.assertTrue(all(int(item["limit"]) == 7 for item in requests))
        self.assertTrue(
            all(
                tuple(item["roles"]) == ("canon", "setting", "outline")
                for item in requests
            )
        )
        self.assertEqual(
            list(range(len(expected_needs))),
            [int(item["need_index"]) for item in contract["needs"]],
        )
        self.assertEqual(
            len(expected_needs),
            contract["retrieval_telemetry"]["query_count"],
        )

    def test_context_builder_selects_legacy_or_v2_search_path(self) -> None:
        prompt = (
            "继续写测试角色甲前往测试城，核对位置、道具、关系、时间与伏笔。"
        )
        expected_needs = decompose_continuity_needs(prompt)

        class RecordingIndex:
            def __init__(self) -> None:
                self.search_calls: list[tuple[str, bool]] = []
                self.search_many_calls: list[
                    list[dict[str, object]]
                ] = []
                self.last_query_count = 0

            @staticmethod
            def candidate(index: int) -> dict[str, object]:
                return {
                    "chunk_id": f"chunk-{index}",
                    "content_sha256": f"sha-{index}",
                    "path": f"正文/第{index + 1}章.md",
                    "ordinal": index,
                    "text": f"need-{index} accepted evidence",
                    "role": "canon",
                    "scope_policy": "infer_and_review",
                    "ingest_policy": "include",
                    "priority": 100,
                    "score": 1.0,
                }

            def search(
                self,
                query: str,
                *,
                use_candidate_cache: bool,
                **_kwargs: object,
            ) -> list[dict[str, object]]:
                index = len(self.search_calls)
                self.search_calls.append(
                    (query, use_candidate_cache)
                )
                self.last_query_count = 1
                return [self.candidate(index)]

            def search_many(
                self,
                requests: object,
            ) -> list[list[dict[str, object]]]:
                normalized = [dict(item) for item in requests]
                self.search_many_calls.append(normalized)
                self.last_query_count = len(normalized)
                return [
                    [self.candidate(index)]
                    for index in range(len(normalized))
                ]

            def last_search_diagnostics(self) -> dict[str, object]:
                return {
                    "query_count": self.last_query_count,
                    "cache_hit_count": 0,
                    "cache_miss_count": self.last_query_count,
                }

        legacy_index = RecordingIndex()
        legacy = ContextContractBuilder(legacy_index).build(
            prompt,
            search_mode="legacy",
            use_candidate_cache=False,
            category_quotas={
                "accepted_authority": 1,
                "current_state": 0,
                "open_loop": 0,
            },
        )
        self.assertEqual(
            len(expected_needs),
            len(legacy_index.search_calls),
        )
        self.assertEqual([], legacy_index.search_many_calls)
        self.assertTrue(
            all(not cache for _query, cache in legacy_index.search_calls)
        )
        self.assertEqual(
            len(expected_needs),
            legacy["retrieval_telemetry"]["query_count"],
        )
        self.assertEqual(
            len(expected_needs),
            legacy["retrieval_telemetry"]["search_calls"],
        )

        v2_index = RecordingIndex()
        v2 = ContextContractBuilder(v2_index).build(
            prompt,
            search_mode="v2",
            use_candidate_cache=True,
            category_quotas={
                "accepted_authority": 1,
                "current_state": 0,
                "open_loop": 0,
            },
        )
        self.assertEqual([], v2_index.search_calls)
        self.assertEqual(1, len(v2_index.search_many_calls))
        self.assertTrue(
            all(
                bool(request["use_candidate_cache"])
                for request in v2_index.search_many_calls[0]
            )
        )
        self.assertEqual(
            len(expected_needs),
            v2["retrieval_telemetry"]["query_count"],
        )

    def test_context_builder_skips_only_exactly_satisfied_authority_needs(
        self,
    ) -> None:
        prompt = (
            "继续写测试角色甲移动到测试城，并核对道具、关系和时间。"
        )
        expected_needs = decompose_continuity_needs(prompt)
        skipped = {
            index
            for index, need in enumerate(expected_needs)
            if need.category in {"current_state", "location"}
        }

        class RecordingIndex:
            def __init__(self) -> None:
                self.requests: list[dict[str, object]] = []

            def search_many(
                self,
                requests: object,
            ) -> list[list[dict[str, object]]]:
                self.requests = [dict(item) for item in requests]
                return [
                    [
                        {
                            "chunk_id": f"chunk-{request['need_index']}",
                            "content_sha256": (
                                f"sha-{request['need_index']}"
                            ),
                            "path": "正文/第一章.md",
                            "ordinal": int(request["need_index"]),
                            "text": "accepted authority evidence",
                            "role": "canon",
                            "scope_policy": "infer_and_review",
                            "ingest_policy": "include",
                            "priority": 100,
                            "score": 1.0,
                            "retrieval_mode": "fts5_bm25",
                        }
                    ]
                    for request in self.requests
                ]

            @staticmethod
            def last_search_diagnostics() -> dict[str, object]:
                return {}

        index = RecordingIndex()
        contract = ContextContractBuilder(index).build(
            prompt,
            search_mode="v2",
            skip_authority_need_indices=skipped,
            exact_state_satisfied_counts={
                "current_state": 2,
                "accepted_authority": 1,
            },
        )

        self.assertEqual(
            [
                index
                for index in range(len(expected_needs))
                if index not in skipped
            ],
            [int(request["need_index"]) for request in index.requests],
        )
        self.assertEqual(
            sorted(skipped),
            contract["exact_state_short_circuit"][
                "skipped_need_indices"
            ],
        )
        self.assertNotIn(
            "current_state",
            contract["missing_mandatory"],
        )
        self.assertNotIn(
            "accepted_authority",
            contract["missing_mandatory"],
        )
        self.assertEqual(1, contract["accepted_authority_satisfied"])
        self.assertEqual(
            len(skipped),
            contract["retrieval_telemetry"][
                "skipped_exact_need_count"
            ],
        )

    def test_search_many_batch_failure_falls_back_per_need_without_losing_bm25(
        self,
    ) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "ALPHA_NEED 测试角色甲正在星桥城东门等待列车。",
            encoding="utf-8",
        )
        (project / "正文" / "第二章.md").write_text(
            "BETA_NEED 青铜钥匙仍由测试角色甲持有。",
            encoding="utf-8",
        )
        single_calls: list[str] = []
        batch_calls: list[list[str]] = []

        def embed(text: str) -> list[float]:
            single_calls.append(text)
            return (
                [1.0, 0.0]
                if "ALPHA_NEED" in text
                else [0.0, 1.0]
            )

        def embed_many(texts: object) -> object:
            values = [str(value) for value in texts]
            batch_calls.append(values)
            raise TimeoutError("fixture batch failure")

        index = AuthorityIndex(
            self.base / "batch-fallback.sqlite3",
            embedding_provider=embed,
            embedding_batch_provider=embed_many,
            embedding_model="fixture-batch-v1",
        )
        index.refresh(project, self.sources())
        single_calls.clear()

        results = index.search_many(
            [
                {
                    "query": "ALPHA_NEED 星桥城",
                    "limit": 2,
                    "roles": ("canon",),
                },
                {
                    "query": "BETA_NEED 青铜钥匙",
                    "limit": 2,
                    "roles": ("canon",),
                },
            ],
            use_candidate_cache=False,
        )
        diagnostics = index.last_search_diagnostics()

        self.assertEqual(
            [["alpha_need 星桥城", "beta_need 青铜钥匙"]],
            batch_calls,
        )
        self.assertEqual(
            ["alpha_need 星桥城", "beta_need 青铜钥匙"],
            single_calls,
        )
        self.assertEqual(1, diagnostics["embedding_batch_calls"])
        self.assertEqual(1, diagnostics["embedding_batch_failures"])
        self.assertEqual(2, diagnostics["embedding_single_fallbacks"])
        self.assertGreaterEqual(diagnostics["embedding_batch_ms"], 0.0)
        self.assertEqual(2, len(results))
        self.assertTrue(all(result for result in results))
        self.assertEqual("正文/第一章.md", results[0][0]["path"])
        self.assertEqual("正文/第二章.md", results[1][0]["path"])
        self.assertTrue(
            all(
                item[0]["embedding_status"] == "ok"
                for item in results
            )
        )

    def test_search_many_runs_exact_single_embeddings_concurrently(self) -> None:
        project = self.make_project()
        for index, marker in enumerate(
            ("EMBED_ALPHA", "EMBED_BETA", "EMBED_GAMMA"),
            start=1,
        ):
            (project / "正文" / f"第{index}章.md").write_text(
                f"{marker} accepted authority evidence.",
                encoding="utf-8",
            )
        lock = threading.Lock()
        active = 0
        max_active = 0

        def embed(text: str) -> list[float]:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return [
                1.0 if marker.casefold() in text else 0.0
                for marker in ("EMBED_ALPHA", "EMBED_BETA", "EMBED_GAMMA")
            ]

        index = AuthorityIndex(
            self.base / "parallel-single-embedding.sqlite3",
            embedding_provider=embed,
            embedding_model="fixture-single-exact-v1",
            embedding_single_max_concurrency=2,
        )
        index.refresh(project, self.sources())

        results = index.search_many(
            [
                {
                    "query": f"{marker} evidence",
                    "limit": 1,
                    "roles": ("canon",),
                    "use_candidate_cache": False,
                }
                for marker in ("EMBED_ALPHA", "EMBED_BETA", "EMBED_GAMMA")
            ]
        )
        diagnostics = index.last_search_diagnostics()

        self.assertEqual(2, max_active)
        self.assertEqual(
            ["正文/第1章.md", "正文/第2章.md", "正文/第3章.md"],
            [candidates[0]["path"] for candidates in results],
        )
        self.assertEqual(3, diagnostics["embedding_single_calls"])
        self.assertEqual(2, diagnostics["embedding_single_max_concurrency"])
        self.assertGreater(
            diagnostics["embedding_single_ms"],
            diagnostics["embedding_single_wall_ms"],
        )

    def test_embedding_base_exceptions_clean_all_flights_and_retry(self) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "FATAL_FLIGHT accepted authority evidence.",
            encoding="utf-8",
        )
        for fatal_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(fatal_type=fatal_type.__name__):
                failed = False

                def embed(text: str) -> list[float]:
                    nonlocal failed
                    if (
                        text.casefold().startswith("fatal_query")
                        and not failed
                    ):
                        failed = True
                        raise fatal_type("fixture fatal embedding")
                    return [1.0, 0.0]

                setattr(
                    embed,
                    "cache_identity",
                    "fixture-fatal-embedding-"
                    + fatal_type.__name__
                    + "-"
                    + os.urandom(8).hex(),
                )
                index = AuthorityIndex(
                    self.base
                    / (
                        "fatal-embedding-"
                        + fatal_type.__name__.casefold()
                        + ".sqlite3"
                    ),
                    embedding_provider=embed,
                    embedding_model="fixture-fatal-embedding-v1",
                    embedding_batch_size=2,
                    embedding_single_max_concurrency=1,
                )
                index.refresh(project, self.sources())
                requests = [
                    {
                        "query": f"FATAL_QUERY_{value}",
                        "limit": 1,
                        "roles": ("canon",),
                        "use_candidate_cache": False,
                    }
                    for value in range(3)
                ]
                specs = [
                    index._normalize_search_spec(
                        request,
                        limit=1,
                        roles=None,
                        scope_policies=None,
                        ingest_policies=("include", "review"),
                        use_candidate_cache=False,
                    )
                    for request in requests
                ]
                embedding_keys = {
                    index._embedding_cache_key(spec.normalized_query)
                    for spec in specs
                }
                search_keys = {
                    index._read_candidate_cache(spec)[1]
                    for spec in specs
                }

                with self.assertRaises(fatal_type):
                    index.search_many(requests)

                self.assertTrue(
                    embedding_keys.isdisjoint(
                        index._query_embedding_flights
                    )
                )
                self.assertTrue(
                    search_keys.isdisjoint(index._search_flights)
                )
                retried = index.search_many(requests)
                self.assertEqual(3, len(retried))
                self.assertTrue(all(result for result in retried))

    def test_search_many_matches_sequential_candidate_and_topk_semantics(
        self,
    ) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "EQUIV_ALPHA 测试角色甲位于测试城东门。",
            encoding="utf-8",
        )
        (project / "设定集" / "交通.md").write_text(
            "EQUIV_BETA 跨层通行只能使用列车。",
            encoding="utf-8",
        )
        index = AuthorityIndex(self.base / "search-many-equivalence.sqlite3")
        index.refresh(project, self.sources())
        requests = [
            {
                "query": "EQUIV_ALPHA 测试城",
                "limit": 1,
                "roles": ("canon",),
                "scope_policies": ("infer_and_review",),
                "ingest_policies": ("include",),
                "use_candidate_cache": False,
            },
            {
                "query": "EQUIV_BETA 列车",
                "limit": 2,
                "roles": ("setting",),
                "scope_policies": ("timeless",),
                "ingest_policies": ("include",),
                "use_candidate_cache": False,
            },
        ]
        expected = [
            index._search_legacy(
                str(request["query"]),
                limit=int(request["limit"]),
                roles=request["roles"],
                scope_policies=request["scope_policies"],
                ingest_policies=request["ingest_policies"],
                use_candidate_cache=False,
            )
            for request in requests
        ]

        actual = index.search_many(requests)

        self.assertEqual(expected, actual)
        self.assertEqual(
            [1, 1],
            [len(candidates) for candidates in actual],
        )
        self.assertEqual(
            ["正文/第一章.md", "设定集/交通.md"],
            [candidates[0]["path"] for candidates in actual],
        )

    def test_search_many_reranks_with_bounded_parallelism_and_stable_join(
        self,
    ) -> None:
        project = self.make_project()
        for index, marker in enumerate(
            ("PARALLEL_ALPHA", "PARALLEL_BETA", "PARALLEL_GAMMA"),
            start=1,
        ):
            (project / "正文" / f"第{index}章.md").write_text(
                f"{marker} accepted authority evidence.",
                encoding="utf-8",
            )
        lock = threading.Lock()
        active = 0
        max_active = 0
        calls: list[str] = []

        def rerank(
            query: str,
            documents: list[str],
            _top_n: int,
        ) -> list[tuple[int, float]]:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                calls.append(query)
            time.sleep(0.04)
            with lock:
                active -= 1
            return [
                (index, float(len(documents) - index))
                for index in range(len(documents))
            ]

        index = AuthorityIndex(
            self.base / "parallel-rerank.sqlite3",
            rerank_provider=rerank,
            rerank_model="fixture-rerank-v1",
            rerank_max_concurrency=2,
        )
        index.refresh(project, self.sources())
        queries = [
            f"{marker} evidence"
            for marker in (
                "PARALLEL_ALPHA",
                "PARALLEL_BETA",
                "PARALLEL_GAMMA",
            )
        ]

        results = index.search_many(
            [
                {
                    "query": query,
                    "limit": 1,
                    "roles": ("canon",),
                    "use_candidate_cache": False,
                }
                for query in queries
            ]
        )
        diagnostics = index.last_search_diagnostics()

        self.assertEqual(2, max_active)
        self.assertCountEqual(
            [query.casefold() for query in queries],
            calls,
        )
        self.assertEqual(
            ["正文/第1章.md", "正文/第2章.md", "正文/第3章.md"],
            [candidates[0]["path"] for candidates in results],
        )
        self.assertGreater(
            diagnostics["rerank_sum_ms"],
            diagnostics["rerank_wall_ms"],
        )

    def test_search_many_rerank_preserves_legacy_provider_order(self) -> None:
        identity = "fixture-rerank-order-" + os.urandom(8).hex()

        def rerank(
            _query: str,
            documents: list[str],
            _top_n: int,
        ) -> list[tuple[int, float]]:
            self.assertEqual(32, len(documents))
            return [
                (1, 0.9),
                (0, 0.8),
                *[
                    (index, 0.8 - index / 100.0)
                    for index in range(2, len(documents))
                ],
            ]

        setattr(rerank, "cache_identity", identity)
        index = AuthorityIndex(
            self.base / "rerank-provider-order.sqlite3",
            rerank_provider=rerank,
            rerank_model="fixture-rerank-order-v1",
        )
        spec = index._normalize_search_spec(
            {
                "query": "RERANK_ORDER",
                "limit": 1,
                "roles": ("canon",),
                "use_candidate_cache": False,
            },
            limit=1,
            roles=None,
            scope_policies=None,
            ingest_policies=("include", "review"),
            use_candidate_cache=False,
        )
        ranked = [
            {
                "text": f"document-{candidate}",
                "path": f"正文/{candidate:02d}.md",
                "ordinal": candidate,
                "priority": 0,
                "score": 1.0 if candidate == 0 else 0.0,
                "base_score": 1.0 if candidate == 0 else 0.0,
                "retrieval_mode": "lexical",
                "rerank_status": "not_called",
                "rerank_rank": None,
                "rerank_score": None,
            }
            for candidate in range(32)
        ]

        completed = index._apply_rerank(spec, ranked)

        self.assertEqual("正文/01.md", completed[0]["path"])
        self.assertGreater(
            float(completed[1]["score"]),
            float(completed[0]["score"]),
        )

    def test_singleflight_collapses_identical_search_across_index_instances(
        self,
    ) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "SINGLEFLIGHT_MARKER 测试角色甲仍在测试城。",
            encoding="utf-8",
        )
        calls = 0
        lock = threading.Lock()

        def embed(_text: str) -> list[float]:
            nonlocal calls
            with lock:
                calls += 1
            time.sleep(0.08)
            return [1.0, 0.0]

        setattr(embed, "cache_identity", "shared-fixture-embedding")
        database = self.base / "shared-singleflight.sqlite3"
        first_index = AuthorityIndex(
            database,
            embedding_provider=embed,
            embedding_model="fixture-singleflight-v1",
        )
        first_index.refresh(project, self.sources())
        calls = 0
        second_index = AuthorityIndex(
            database,
            embedding_provider=embed,
            embedding_model="fixture-singleflight-v1",
        )
        barrier = threading.Barrier(3)
        results: list[list[dict[str, object]]] = []
        diagnostics: list[dict[str, object]] = []

        def run(index: AuthorityIndex) -> None:
            barrier.wait()
            results.append(
                index.search(
                    "SINGLEFLIGHT_MARKER 测试城",
                    limit=1,
                    roles=("canon",),
                    use_candidate_cache=False,
                )
            )
            diagnostics.append(index.last_search_diagnostics())

        threads = [
            threading.Thread(target=run, args=(first_index,)),
            threading.Thread(target=run, args=(second_index,)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(1, calls)
        self.assertEqual(2, len(results))
        self.assertEqual(results[0], results[1])
        self.assertEqual(
            1,
            sum(
                int(item["search_singleflight_waits"])
                for item in diagnostics
            ),
        )

    def test_accepted_commit_projects_three_memories_and_three_summary_levels(self) -> None:
        memory = LayeredMemoryStore(self.base / "layered.sqlite3")
        summaries = AcceptedSummaryStore(self.base / "summaries.sqlite3")
        rejected = {
            "commit_id": "rejected-1",
            "canon_status": "rejected",
            "artifact_stage": "draft",
            "text": "不应进入任何记忆。",
            "current_state": ["错误状态"],
        }
        self.assertEqual(
            {"working": 0, "episodic": 0, "semantic": 0},
            memory.project_accepted_commit(rejected),
        )
        self.assertFalse(summaries.project_commit(rejected)["projected"])
        for chapter_no in (1, 2):
            commit = {
                "commit_id": f"accepted-{chapter_no}",
                "canon_status": "accepted",
                "artifact_stage": "final",
                "branch_id": "main",
                "chapter_no": chapter_no,
                "arc_id": "arc-a",
                "volume_id": "volume-a",
                "text": f"第{chapter_no}章发生了不可逆事件。",
                "current_state": [f"测试角色甲状态更新为{chapter_no}。"],
                "open_loops": [f"承诺{chapter_no}等待兑现。"],
                "events": [f"事件{chapter_no}改变了关系。"],
                "semantic_facts": [f"世界规则{chapter_no}已经确认。"],
            }
            counts = memory.project_accepted_commit(commit)
            self.assertGreater(counts["working"], 0)
            self.assertGreater(counts["episodic"], 0)
            self.assertGreater(counts["semantic"], 0)
            self.assertTrue(summaries.project_commit(commit)["projected"])
        layer_counts = memory.counts()
        self.assertGreaterEqual(layer_counts["working"], 4)
        self.assertGreaterEqual(layer_counts["episodic"], 4)
        self.assertGreaterEqual(layer_counts["semantic"], 2)
        self.assertEqual(2, len(summaries.list("chapter")))
        self.assertEqual(1, len(summaries.list("arc")))
        self.assertEqual(1, len(summaries.list("volume")))

    def test_context_retrieves_historical_episodic_memory_without_authority_hit(
        self,
    ) -> None:
        memory = LayeredMemoryStore(self.base / "episodic-only.sqlite3")
        self.assertTrue(
            memory.add(
                layer="episodic",
                category="event",
                content="黑潮钟摆曾在旧港倒转一次，守门人因此失去七年记忆。",
                source_commit_id="accepted-episodic-only",
                canon_status="accepted",
                branch_id="main",
                chapter_no=7,
                arc_id="arc-old-port",
                volume_id="volume-one",
                scope="historical",
            )
        )
        builder = ContextContractBuilder(
            AuthorityIndex(self.base / "episodic-empty-authority.sqlite3"),
            memory_store=memory,
        )
        contract = builder.build(
            "主角再次听见黑潮钟摆时，应当承接哪件旧事？",
            task="prose",
            max_context_chars=700,
            branch_id="main",
            chapter_no=12,
            arc_id="arc-old-port",
            volume_id="volume-one",
        )
        self.assertIn("episodic_memory", contract["sections"])
        item = contract["sections"]["episodic_memory"][0]
        self.assertEqual("historical", item["scope"])
        self.assertEqual(7, item["chapter_no"])
        self.assertIn("scope=historical", contract["context_text"])
        self.assertIn("chapter=7", contract["context_text"])
        self.assertTrue(contract["within_budget"])

    def test_summary_prunes_superseded_commit_when_coordinates_change(
        self,
    ) -> None:
        summaries = AcceptedSummaryStore(self.base / "summary-prune.sqlite3")
        old_commit = {
            "commit_id": "accepted-old-coordinate",
            "canon_status": "accepted",
            "artifact_stage": "final",
            "branch_id": "main",
            "chapter_no": 3,
            "arc_id": "arc-old",
            "volume_id": "volume-old",
            "summary": "旧坐标摘要。",
        }
        new_commit = {
            **old_commit,
            "commit_id": "accepted-new-coordinate",
            "chapter_no": 4,
            "arc_id": "arc-new",
            "volume_id": "volume-new",
            "summary": "新坐标摘要。",
        }
        summaries.project_commit(old_commit)
        summaries.project_commit(new_commit)
        pruned = summaries.prune_to_source_commits(
            {new_commit["commit_id"]}
        )
        self.assertEqual(1, pruned["chapters_removed"])
        chapters = summaries.list("chapter")
        self.assertEqual(1, len(chapters))
        self.assertEqual(
            [new_commit["commit_id"]],
            chapters[0]["source_commits"],
        )
        self.assertEqual(1, len(summaries.list("arc")))
        self.assertEqual(1, len(summaries.list("volume")))

    def test_context_retrieves_semantic_world_rule_without_authority_hit(
        self,
    ) -> None:
        memory = LayeredMemoryStore(self.base / "semantic-only.sqlite3")
        self.assertTrue(
            memory.add(
                layer="semantic",
                category="world_rule",
                content="逆星渡口只承认由失名者支付的影子通行税。",
                source_commit_id="accepted-semantic-only",
                canon_status="accepted",
                branch_id="main",
                scope="timeless",
            )
        )
        builder = ContextContractBuilder(
            AuthorityIndex(self.base / "semantic-empty-authority.sqlite3"),
            memory_store=memory,
        )
        contract = builder.build(
            "安排角色通过逆星渡口，核对影子通行税规则。",
            task="outline",
            max_context_chars=640,
            branch_id="main",
        )
        self.assertIn("semantic_memory", contract["sections"])
        item = contract["sections"]["semantic_memory"][0]
        self.assertEqual("world_rule", item["memory_category"])
        self.assertEqual("timeless", item["scope"])
        self.assertIn("memory:semantic", contract["context_text"])
        self.assertTrue(contract["within_budget"])

    def test_context_retrieves_current_volume_summary_with_scope_labels(
        self,
    ) -> None:
        summaries = AcceptedSummaryStore(self.base / "volume-summary.sqlite3")
        for chapter_no in (1, 2):
            projected = summaries.project_commit(
                {
                    "commit_id": f"accepted-volume-{chapter_no}",
                    "canon_status": "accepted",
                    "branch_id": "main",
                    "chapter_no": chapter_no,
                    "arc_id": "arc-thunder-court",
                    "volume_id": "volume-thunder",
                    "text": (
                        f"第{chapter_no}章确认远雷议会以断弦投票裁定王位归属。"
                    ),
                    "events": [],
                }
            )
            self.assertTrue(projected["projected"])
        builder = ContextContractBuilder(
            AuthorityIndex(self.base / "summary-empty-authority.sqlite3"),
            summary_store=summaries,
        )
        contract = builder.build(
            "规划远雷议会的断弦投票如何在本卷高潮兑现。",
            task="outline",
            max_context_chars=800,
            branch_id="main",
            chapter_no=3,
            arc_id="arc-thunder-court",
            volume_id="volume-thunder",
        )
        self.assertIn("volume_summary", contract["sections"])
        item = contract["sections"]["volume_summary"][0]
        self.assertEqual("volume", item["level"])
        self.assertEqual("accepted", item["scope"])
        self.assertEqual("volume-thunder", item["volume_id"])
        self.assertIn("summary:volume", contract["context_text"])
        self.assertIn("volume=volume-thunder", contract["context_text"])
        self.assertTrue(contract["within_budget"])

    def test_projection_retry_replay_is_independent_and_hash_stable(self) -> None:
        journal = ProjectionJournal(self.base / "projection.sqlite3")
        commit = {
            "commit_id": "accepted-projection-1",
            "canon_status": "accepted",
            "artifact_stage": "final",
            "chapter_no": 7,
            "events": [{"type": "move", "to": "测试城"}],
        }
        attempts = {"summary": 0}

        def flaky(payload: dict[str, object]) -> dict[str, object]:
            attempts["summary"] += 1
            if attempts["summary"] == 1:
                raise RuntimeError("fixture projection failure")
            return {
                "chapter": payload["chapter_no"],
                "updated_at": f"attempt-{attempts['summary']}",
                "run_id": f"volatile-{attempts['summary']}",
            }

        with self.assertRaises(ProjectionRunError) as raised:
            journal.run("summary", commit, flaky)
        long_error = "投影错误" * 300
        with closing(sqlite3.connect(journal.database_path)) as connection:
            connection.execute(
                """
                UPDATE projection_runs
                SET error_text = ?
                WHERE run_id = ?
                """,
                (long_error, raised.exception.run_id),
            )
            connection.commit()
        retry = journal.retry(raised.exception.run_id, flaky)
        self.assertEqual("succeeded", retry["status"])

        projectors = {
            name: (
                lambda payload, projection=name: {
                    "projection": projection,
                    "chapter": payload["chapter_no"],
                    "updated_at": "volatile-first",
                    "run_id": "volatile-first",
                }
            )
            for name in PROJECTION_NAMES
        }
        first = journal.replay(commit, projectors)
        second_projectors = {
            name: (
                lambda payload, projection=name: {
                    "projection": projection,
                    "chapter": payload["chapter_no"],
                    "updated_at": "volatile-second",
                    "run_id": "volatile-second",
                }
            )
            for name in PROJECTION_NAMES
        }
        second = journal.replay(commit, second_projectors)
        self.assertEqual(set(PROJECTION_NAMES), set(first))
        for name in PROJECTION_NAMES:
            self.assertEqual(
                first[name]["output_sha256"],
                second[name]["output_sha256"],
            )
        self.assertEqual(
            stable_normalized_hash({"value": 1, "updated_at": "one", "run_id": "a"}),
            stable_normalized_hash({"run_id": "b", "updated_at": "two", "value": 1}),
        )
        run_names = {row["projection_name"] for row in journal.runs()}
        self.assertEqual(set(PROJECTION_NAMES), run_names)
        summaries = journal.runs(
            include_payload=False,
            limit=2,
            newest_first=True,
        )
        self.assertEqual(2, len(summaries))
        self.assertNotIn("input_json", summaries[0])
        self.assertGreater(summaries[0]["input_bytes"], 0)
        self.assertEqual(len(journal.runs()), journal.run_count())
        failed_summary = journal.inspect_run(
            raised.exception.run_id,
            include_payload=False,
        )
        self.assertEqual(512, len(failed_summary["error_text"]))
        self.assertEqual(
            len(long_error.encode("utf-8")),
            failed_summary["error_bytes"],
        )
        self.assertEqual(1, failed_summary["error_truncated"])
        full_failure = journal.inspect_run(
            raised.exception.run_id,
            include_payload=True,
        )
        self.assertEqual(long_error, full_failure["error_text"])
        short_multibyte_error = "错" * 200
        with closing(sqlite3.connect(journal.database_path)) as connection:
            connection.execute(
                """
                UPDATE projection_runs
                SET error_text = ?
                WHERE run_id = ?
                """,
                (short_multibyte_error, raised.exception.run_id),
            )
            connection.commit()
        short_multibyte_summary = journal.inspect_run(
            raised.exception.run_id,
            include_payload=False,
        )
        self.assertEqual(
            short_multibyte_error,
            short_multibyte_summary["error_text"],
        )
        self.assertEqual(
            len(short_multibyte_error.encode("utf-8")),
            short_multibyte_summary["error_bytes"],
        )
        self.assertEqual(0, short_multibyte_summary["error_truncated"])
        inspected = journal.inspect_run(
            summaries[0]["run_id"],
            include_payload=True,
        )
        self.assertIn("input_json", inspected)

    def test_degraded_projection_is_retryable_and_not_cached_as_success(
        self,
    ) -> None:
        journal = ProjectionJournal(self.base / "projection-degraded.sqlite3")
        commit = {
            "commit_id": "accepted-projection-degraded",
            "canon_status": "accepted",
            "artifact_stage": "final",
            "chapter_no": 8,
        }
        degraded = journal.run(
            "vector",
            commit,
            lambda _payload: {
                "status": "degraded",
                "projected": False,
                "lexical_ready": True,
            },
        )
        self.assertEqual("degraded", degraded["status"])

        direct_rerun_calls = 0

        def still_degraded(_payload: dict[str, object]) -> dict[str, object]:
            nonlocal direct_rerun_calls
            direct_rerun_calls += 1
            return {
                "status": "degraded",
                "projected": False,
                "lexical_ready": True,
            }

        direct_rerun = journal.run("vector", commit, still_degraded)
        self.assertEqual("degraded", direct_rerun["status"])
        self.assertIsNotNone(direct_rerun["run_id"])
        self.assertEqual(1, direct_rerun_calls)

        retried = journal.retry(
            direct_rerun["run_id"],
            lambda _payload: {
                "status": "success",
                "projected": True,
                "lexical_ready": True,
            },
        )
        self.assertEqual("succeeded", retried["status"])
        cached = journal.run(
            "vector",
            commit,
            lambda _payload: self.fail("successful retry should be cached"),
        )
        self.assertEqual("cached", cached["status"])

    def test_retention_prunes_only_rebuildable_cache_and_projection_runs(self) -> None:
        project = self.make_project()
        (project / "正文" / "第一章.md").write_text(
            "RETENTION_MARKER 测试角色甲仍在测试城站台。",
            encoding="utf-8",
        )
        index = AuthorityIndex(self.base / "retention-index.sqlite3")
        index.refresh(project, self.sources())
        before = index.schema_info()
        for query in ("RETENTION_MARKER", "测试角色甲", "测试城", "站台"):
            index.search(query, limit=3)
        pruned = index.prune_derived_cache(keep_candidate_queries=1)
        after = index.schema_info()
        self.assertGreaterEqual(pruned["candidate_queries_removed"], 3)
        self.assertEqual(before["file_count"], after["file_count"])
        self.assertEqual(before["chunk_count"], after["chunk_count"])
        with closing(sqlite3.connect(index.database_path)) as connection:
            cache_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM rerank_candidate_cache"
                ).fetchone()[0]
            )
        self.assertEqual(1, cache_count)

        journal = ProjectionJournal(self.base / "retention-projections.sqlite3")
        for number in range(4):
            journal.run(
                "snapshot",
                {
                    "commit_id": f"retention-{number}",
                    "canon_status": "accepted",
                    "artifact_stage": "final",
                    "chapter_no": number + 1,
                },
                lambda payload: {"chapter_no": payload["chapter_no"]},
            )
        self.assertEqual(4, len(journal.runs("snapshot")))
        self.assertEqual(
            3,
            journal.prune_derived_runs(keep_successful_per_projection=1),
        )
        self.assertEqual(1, len(journal.runs("snapshot")))

    def test_method_pack_filters_tasks_and_rejected_never_learns(self) -> None:
        pack = WebnovelMethodPack()
        self.assertEqual(15, len(pack.cards))
        for card in pack.cards:
            self.assertRegex(card["source"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(card["misuse_boundaries"])
        outline = pack.retrieve(
            "卷末兑现和成长资源",
            genre="玄幻",
            artifact_stage="outline",
            task="outline",
            continuity_risks=["volume_boundary"],
        )
        prose = pack.retrieve(
            "章首承接上一章动作",
            genre="玄幻",
            artifact_stage="draft",
            task="prose",
            continuity_risks=["chapter_boundary"],
        )
        revision = pack.retrieve(
            "检查近章重复结构",
            genre="玄幻",
            artifact_stage="final",
            task="revision",
            continuity_risks=["repetition"],
        )
        self.assertEqual("volume_climax", outline[0]["id"])
        self.assertEqual("chapter_opening_continuity", prose[0]["id"])
        self.assertEqual("repetition_control", revision[0]["id"])
        public_guidance = pack.render_guidance(prose, expose_internal_checks=False)
        self.assertNotIn("内部校验", public_guidance)
        self.assertNotIn("误用边界", public_guidance)

        patterns = ProjectPatternStore(self.base / "craft-memory.sqlite3")
        rejected = patterns.learn(
            {
                "commit_id": "draft-r",
                "canon_status": "rejected",
                "artifact_stage": "draft",
                "text": "错误模式",
            }
        )
        proposed = patterns.learn(
            {
                "commit_id": "draft-p",
                "canon_status": "proposed",
                "artifact_stage": "final",
                "text": "未验收模式",
            }
        )
        accepted_draft = patterns.learn(
            {
                "commit_id": "accepted-draft",
                "canon_status": "accepted",
                "artifact_stage": "draft",
                "text": "尚未定稿模式",
            }
        )
        self.assertFalse(rejected["learned"])
        self.assertFalse(proposed["learned"])
        self.assertFalse(accepted_draft["learned"])
        self.assertEqual(0, patterns.count())
        learned = patterns.learn(
            {
                "commit_id": "accepted-final",
                "canon_status": "accepted",
                "artifact_stage": "final",
                "genre": "玄幻",
                "task": "prose",
                "success_pattern": "先让资源困局可感知，再由主角用旧伏笔完成反转。",
                "craft_signals": {"reader_retention": "positive"},
            }
        )
        self.assertTrue(learned["learned"])
        self.assertEqual(1, patterns.count())

    def test_method_pack_schema_version_requires_exact_json_integer(self) -> None:
        source = PLUGIN_ROOT / "knowledge" / "webnovel_methods.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        for index, malformed in enumerate((True, 1.0, "1"), start=1):
            with self.subTest(schema_version=repr(malformed)):
                candidate = dict(payload)
                candidate["schema_version"] = malformed
                path = self.base / f"method-pack-version-{index}.json"
                path.write_text(
                    json.dumps(candidate, ensure_ascii=False),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "method pack schema version mismatch",
                ):
                    WebnovelMethodPack(path)

    def test_versioned_annotation_benchmark_is_reproducible(self) -> None:
        manifest = (
            PLUGIN_ROOT
            / "benchmarks"
            / "fixtures"
            / "longform_annotations.v1.jsonl"
        )
        validation = validate_annotation_manifest(manifest)
        first = run_annotation_benchmark(manifest)
        second = run_annotation_benchmark(manifest)
        self.assertGreaterEqual(validation["case_count"], 200)
        self.assertEqual(first, second)
        self.assertEqual(
            sum(validation["category_positive_counts"].values()),
            first["tp"],
        )
        self.assertEqual(0, first["fp"])
        self.assertEqual(0, first["fn"])
        self.assertEqual(validation["zero_delta_case_count"], first["zero"])
        self.assertEqual(
            validation["quarantine_expected_count"],
            first["quarantine"],
        )
        self.assertEqual(1.0, first["accepted_delta_precision"])
        self.assertEqual(1.0, first["accepted_delta_recall"])
        self.assertEqual(1.0, first["zero_delta_accuracy"])
        self.assertEqual(1.0, first["quarantine_recall"])
        self.assertEqual(0, first["quarantine_fp"])
        self.assertEqual(0, first["quarantine_fn"])
        self.assertEqual(
            validation["continuity_quarantine_expected_count"],
            first["quarantine_stage_counts"]["continuity"],
        )
        self.assertEqual(
            validation["semantic_quarantine_expected_count"],
            first["quarantine_stage_counts"]["semantic"],
        )
        self.assertEqual(1.0, first["entity_resolution_accuracy"])
        self.assertEqual(1.0, first["alias_resolution_accuracy"])
        self.assertGreaterEqual(first["alias_resolution_total"], 40)
        self.assertEqual(
            first["proposal_candidate_count"],
            first["validator_invocations"]["continuity"],
        )
        for category in ("location", "inventory", "story_time", "relation"):
            self.assertGreaterEqual(
                validation["category_positive_counts"][category],
                20,
            )
            self.assertEqual(
                1.0,
                first["category_metrics"][category]["recall"],
            )
        self.assertRegex(first["corpus_sha256"], r"^[0-9a-f]{64}$")

    def test_benchmark_requires_exact_json_integer_versions(self) -> None:
        manifest = (
            PLUGIN_ROOT
            / "benchmarks"
            / "fixtures"
            / "longform_annotations.v1.jsonl"
        )
        records = [
            json.loads(line)
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        accepted = next(
            record
            for record in records
            if record["case_kind"] == "accepted_delta"
        )
        extracted = extract_proposal_candidates(accepted["assistant_text"])[0]
        block_start, block_end = extracted["block_span"]

        for invalid_version in (True, 1.0, "1"):
            with self.subTest(
                boundary="proposal_version",
                value=repr(invalid_version),
            ):
                proposal = json.loads(
                    json.dumps(extracted["proposal"], ensure_ascii=False)
                )
                proposal["proposal_version"] = invalid_version
                mutated = dict(accepted)
                mutated["assistant_text"] = (
                    accepted["assistant_text"][:block_start]
                    + "<plot-delta>\n"
                    + json.dumps(
                        proposal,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n</plot-delta>"
                    + accepted["assistant_text"][block_end:]
                )
                evaluation = evaluate_annotation_record(mutated)
                self.assertEqual([], evaluation["accepted"])
                self.assertEqual(1, len(evaluation["quarantined"]))
                self.assertEqual(
                    "UNSUPPORTED_PROPOSAL_VERSION",
                    evaluation["quarantined"][0]["code"],
                )

        for invalid_version in (True, 1.0, "1"):
            with self.subTest(
                boundary="manifest_version",
                value=repr(invalid_version),
            ):
                mutated_records = json.loads(
                    json.dumps(records, ensure_ascii=False)
                )
                mutated_records[0]["manifest_version"] = invalid_version
                mutated_path = self.base / "invalid-manifest-version.jsonl"
                mutated_path.write_text(
                    "".join(
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                        for record in mutated_records
                    ),
                    encoding="utf-8",
                    newline="\n",
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "case 1 has unsupported manifest version",
                ):
                    validate_annotation_manifest(mutated_path)

    def test_benchmark_evidence_span_requires_exact_json_integers(self) -> None:
        manifest = (
            PLUGIN_ROOT
            / "benchmarks"
            / "fixtures"
            / "longform_annotations.v1.jsonl"
        )
        accepted = next(
            json.loads(line)
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line)["case_kind"] == "accepted_delta"
        )
        extracted = extract_proposal_candidates(accepted["assistant_text"])[0]
        block_start, block_end = extracted["block_span"]

        for field, invalid_value in (
            ("start", False),
            ("end", True),
            ("start", 0.0),
            ("end", 22.0),
            ("start", 1.0),
            ("end", 1.0),
            ("start", 0.9),
            ("end", 22.9),
            ("start", "0"),
            ("end", "22"),
        ):
            with self.subTest(field=field, value=repr(invalid_value)):
                proposal = json.loads(
                    json.dumps(extracted["proposal"], ensure_ascii=False)
                )
                proposal["evidence"][field] = invalid_value
                mutated = dict(accepted)
                mutated["assistant_text"] = (
                    accepted["assistant_text"][:block_start]
                    + "<plot-delta>\n"
                    + json.dumps(
                        proposal,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n</plot-delta>"
                    + accepted["assistant_text"][block_end:]
                )
                evaluation = evaluate_annotation_record(mutated)
                self.assertEqual([], evaluation["accepted"])
                self.assertEqual(1, len(evaluation["quarantined"]))
                self.assertEqual(
                    "EVIDENCE_SPAN_INVALID",
                    evaluation["quarantined"][0]["code"],
                )

    def test_annotation_benchmark_detects_missing_marker_and_tampered_evidence(
        self,
    ) -> None:
        manifest = (
            PLUGIN_ROOT
            / "benchmarks"
            / "fixtures"
            / "longform_annotations.v1.jsonl"
        )
        records = [
            json.loads(line)
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        accepted_index = next(
            index
            for index, record in enumerate(records)
            if record["case_kind"] == "accepted_delta"
        )

        missing_marker_records = json.loads(
            json.dumps(records, ensure_ascii=False)
        )
        marker_text = missing_marker_records[accepted_index]["assistant_text"]
        missing_marker_records[accepted_index]["assistant_text"] = (
            marker_text.replace("<plot-delta>", "", 1).replace(
                "</plot-delta>",
                "",
                1,
            )
        )
        missing_marker_path = self.base / "missing-marker.jsonl"
        missing_marker_path.write_text(
            "".join(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
                for record in missing_marker_records
            ),
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(ValueError, "proposal ids"):
            run_annotation_benchmark(missing_marker_path)

        tampered_records = json.loads(json.dumps(records, ensure_ascii=False))
        extracted = extract_proposal_candidates(
            tampered_records[accepted_index]["assistant_text"]
        )
        evidence_hash = extracted[0]["proposal"]["evidence"]["sha256"]
        tampered_records[accepted_index]["assistant_text"] = tampered_records[
            accepted_index
        ]["assistant_text"].replace(evidence_hash, "0" * 64, 1)
        tampered_path = self.base / "tampered-evidence.jsonl"
        tampered_path.write_text(
            "".join(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
                for record in tampered_records
            ),
            encoding="utf-8",
            newline="\n",
        )
        tampered_result = run_annotation_benchmark(tampered_path)
        self.assertEqual(1, tampered_result["fn"])
        self.assertEqual(1, tampered_result["quarantine_fp"])
        self.assertEqual(
            1,
            tampered_result["quarantine_reason_counts"][
                "EVIDENCE_HASH_MISMATCH"
            ],
        )

    def test_dangerous_delta_runs_through_continuity_validator(self) -> None:
        manifest = (
            PLUGIN_ROOT
            / "benchmarks"
            / "fixtures"
            / "longform_annotations.v1.jsonl"
        )
        records = [
            json.loads(line)
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        dangerous = next(
            record
            for record in records
            if record["case_kind"] == "dangerous_delta"
            and record["category"] == "inventory"
        )
        extracted = extract_proposal_candidates(dangerous["assistant_text"])
        proposal = extracted[0]["proposal"]
        self.assertNotIn("quarantined", proposal)
        self.assertNotIn("dangerous_delta", proposal)

        evaluation = evaluate_annotation_record(dangerous)
        self.assertEqual([], evaluation["accepted"])
        self.assertEqual(1, evaluation["validator_invocations"]["continuity"])
        self.assertEqual(1, len(evaluation["quarantined"]))
        quarantined = evaluation["quarantined"][0]
        self.assertEqual("continuity", quarantined["validator_stage"])
        self.assertEqual(
            "INVENTORY_TRANSFER_SAME_OWNER",
            quarantined["code"],
        )
        self.assertIn("continuity", quarantined["validator_trace"])

    def test_500_chapter_fixture_respects_context_budget_and_zero_reparse(self) -> None:
        fixture = (
            PLUGIN_ROOT / "benchmarks" / "fixtures" / "chapters_500.v1.jsonl"
        )
        records = [
            json.loads(line)
            for line in fixture.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(500, len(records))
        project = self.make_project()
        for record in records:
            path = project / Path(record["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(record["text"], encoding="utf-8")
        index = AuthorityIndex(
            self.base / "chapters-500.sqlite3",
            max_chunk_chars=600,
        )
        first = index.refresh(project, self.sources())
        self.assertEqual(500, first["files_parsed"])
        second = index.refresh(project, self.sources())
        self.assertEqual(500, second["files_hashed"])
        self.assertEqual(500, second["files_unchanged"])
        self.assertEqual(0, second["files_parsed"])
        self.assertEqual(0, second["chunks_written"])
        builder = ContextContractBuilder(index)
        contract = builder.build(
            "查询 CHAPTERMARKER0500 的当前位置、道具和 DEBT0500 兑现窗口。",
            task="revision",
            max_context_chars=800,
            authority_limit=8,
        )
        self.assertTrue(contract["within_budget"])
        self.assertLessEqual(contract["context_chars"], 800)
        self.assertIn("CHAPTERMARKER0500", contract["context_text"])


if __name__ == "__main__":
    unittest.main()
