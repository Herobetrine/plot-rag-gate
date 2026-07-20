from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from longform import AuthorityIndex  # noqa: E402
import v1_runtime  # noqa: E402


class LongformEmbeddingPolicyTests(unittest.TestCase):
    def test_existing_authority_vectors_are_used_when_craft_embedding_is_enabled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".plot-rag").mkdir()
            (root / "正文").mkdir()
            (root / "正文" / "第一章.md").write_text(
                "火焰巨兽栖息在赤红山脉深处。",
                encoding="utf-8",
            )
            source = {
                "glob": "正文/**/*.md",
                "role": "canon",
                "priority": 100,
                "scope_policy": "infer_and_review",
                "ingest_policy": "include",
            }
            (root / ".plot-rag" / "config.json").write_text(
                json.dumps(
                    {
                        "config_version": 3,
                        "enabled": True,
                        "lifecycle": {
                            "strict": True,
                            "index_embeddings_on_prepare": False,
                        },
                        "authority_sources": [source],
                        "craft": {
                            "enabled": True,
                            "auto_retrieve": True,
                            "use_embedding": True,
                            "use_rerank": True,
                        },
                        "remote": {
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
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            seed_index = AuthorityIndex(
                v1_runtime._authority_index_path(root),
                embedding_provider=lambda _text: [0.0, 1.0],
                embedding_model="mock-embedding-v1",
            )
            seeded = seed_index.refresh(root, [source])
            self.assertEqual(1, seeded["embedding_calls"])
            self.assertEqual(1, seed_index.schema_info()["vector_count"])

            embedding_queries: list[list[str]] = []
            rerank_queries: list[tuple[str, list[str], int]] = []

            def fake_embedding_call(
                _config: object,
                documents: list[str],
            ) -> tuple[list[list[float]], dict[str, object]]:
                embedding_queries.append(list(documents))
                return (
                    [[0.0, 1.0] for _document in documents],
                    {"status": "ok"},
                )

            def fake_rerank_call(
                _config: object,
                query: str,
                documents: list[str],
                top_n: int,
            ) -> tuple[list[tuple[int, float]], dict[str, object]]:
                rerank_queries.append((query, list(documents), top_n))
                return (
                    [
                        (index, 1.0 - index / max(1, len(documents)))
                        for index in range(min(top_n, len(documents)))
                    ],
                    {"status": "ok"},
                )

            with (
                patch.object(
                    v1_runtime.state_rag,
                    "_embedding_call",
                    side_effect=fake_embedding_call,
                ),
                patch.object(
                    v1_runtime.state_rag,
                    "_rerank_call",
                    side_effect=fake_rerank_call,
                ),
            ):
                result = v1_runtime.build_longform_context(
                    root,
                    "那只会喷火的危险生物在哪里？",
                    artifact_context={
                        "artifact_stage": "outline",
                        "task": "outline",
                        "branch_id": "main",
                    },
                    max_context_chars=1200,
                )

            # The lifecycle flag controls whether prepare refreshes all source
            # embeddings. It must not disable semantic querying of vectors that
            # already exist when craft.use_embedding is enabled.
            self.assertFalse(result["index"]["with_embeddings"])
            self.assertTrue(embedding_queries)
            self.assertTrue(rerank_queries)
            self.assertFalse(
                result["index"]["prepare_refresh"][
                    "embedding_generation_requested"
                ]
            )
            self.assertFalse(
                result["index"]["prepare_refresh"]["schema"][
                    "embedding_enabled"
                ]
            )
            self.assertEqual(
                {
                    "embedding_requested": True,
                    "embedding_enabled": True,
                    "embedding_model": "mock-embedding-v1",
                    "rerank_requested": True,
                    "rerank_enabled": True,
                    "rerank_model": "mock-rerank-v1",
                    "chosen_path": "v1",
                },
                result["index"]["query_policy"],
            )
            self.assertTrue(
                result["index"]["query_schema"]["embedding_enabled"]
            )
            self.assertTrue(
                result["index"]["query_schema"]["rerank_enabled"]
            )
            self.assertEqual(
                1,
                result["index"]["query_schema"]["vector_count"],
            )

            authority_hits = [
                item
                for items in result["contract"]["sections"].values()
                for item in items
                if item.get("path") == "正文/第一章.md"
            ]
            self.assertTrue(authority_hits)
            hit = authority_hits[0]
            self.assertEqual("ok", hit["embedding_status"])
            self.assertEqual("mock-embedding-v1", hit["embedding_model"])
            self.assertEqual("ok", hit["rerank_status"])
            self.assertEqual("mock-rerank-v1", hit["rerank_model"])
            self.assertIsNotNone(hit["vector_score"])
            self.assertTrue(
                hit["retrieval_mode"].startswith("reranked_"),
                hit["retrieval_mode"],
            )
            observation = result["index"]["query_observation"]
            self.assertGreaterEqual(observation["authority_candidate_count"], 1)
            self.assertGreaterEqual(observation["vector_candidate_count"], 1)
            self.assertGreaterEqual(
                observation["reranked_candidate_count"],
                1,
            )
            self.assertEqual(["ok"], observation["embedding_statuses"])
            self.assertEqual(["ok"], observation["rerank_statuses"])
            self.assertTrue(
                all(
                    mode.startswith("reranked_")
                    for mode in observation["retrieval_modes"]
                )
            )


if __name__ == "__main__":
    unittest.main()
