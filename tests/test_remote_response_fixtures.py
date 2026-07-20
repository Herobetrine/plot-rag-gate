from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest import mock

from scripts import state_rag


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    PLUGIN_ROOT
    / "benchmarks"
    / "fixtures"
    / "remote_responses.v1.json"
)


def _service(name: str) -> state_rag.ServiceConfig:
    endpoints = {
        "embedding": "embeddings",
        "rerank": "rerank",
        "extract": "chat/completions",
    }
    return state_rag.ServiceConfig(
        name=name,
        enabled=True,
        base_url="https://api.siliconflow.cn/v1",
        model=f"fixture-{name}-v1",
        api_key_env="SILICONFLOW_API_KEY",
        api_key_required=False,
        endpoint=endpoints[name],
        timeout_seconds=1.0,
        max_tokens=1024,
    )


class RemoteResponseFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw_text = FIXTURE_PATH.read_text(encoding="utf-8")
        cls.payload = json.loads(cls.raw_text)
        cls.cases = {
            str(case["id"]): case
            for case in cls.payload["cases"]
        }

    def test_fixture_is_redacted_complete_and_uniquely_identified(self) -> None:
        self.assertEqual(
            "plot-rag-remote-responses/v1",
            self.payload["schema_version"],
        )
        self.assertIs(True, self.payload["redacted"])
        self.assertEqual(8, len(self.cases))
        self.assertEqual(8, len(self.payload["cases"]))
        self.assertEqual(
            {
                "embedding.ok.indexed",
                "embedding.error.invalid-component",
                "rerank.ok.indexed",
                "rerank.error.duplicate-index",
                "chat-json.ok.delta-v4",
                "chat-json.error.invalid-content",
                "chat-tool.ok.delta-v4",
                "chat-tool.error.wrong-name",
            },
            set(self.cases),
        )
        self.assertIsNone(
            re.search(
                r"(?i)(?:bearer\s+|api[_-]?key[\"'=:\s]+|sk-)"
                r"[A-Za-z0-9._~+/=-]{8,}",
                self.raw_text,
            )
        )

    def test_embedding_fixtures_replay_through_production_decoder(self) -> None:
        service = _service("embedding")
        good = self.cases["embedding.ok.indexed"]
        with mock.patch.object(
            state_rag,
            "_remote_json",
            return_value=(good["response"], {"status": "ok"}),
        ):
            vectors, status = state_rag._embedding_call(
                service,
                ["query-a", "query-b"],
            )
        self.assertEqual([[1.0, 0.0], [0.0, 1.0]], vectors)
        self.assertEqual("ok", status["status"])

        bad = self.cases["embedding.error.invalid-component"]
        with mock.patch.object(
            state_rag,
            "_remote_json",
            return_value=(bad["response"], {"status": "ok"}),
        ):
            with self.assertRaisesRegex(
                state_rag.StateRagError,
                re.escape(str(bad["error_code"])),
            ):
                state_rag._embedding_call(service, ["query"])

    def test_rerank_fixtures_replay_through_production_decoder(self) -> None:
        service = _service("rerank")
        good = self.cases["rerank.ok.indexed"]
        with mock.patch.object(
            state_rag,
            "_remote_json",
            return_value=(good["response"], {"status": "ok"}),
        ):
            ranked, status = state_rag._rerank_call(
                service,
                "query",
                ["a", "b", "c"],
                3,
            )
        self.assertEqual([(2, 0.91), (0, 0.73)], ranked)
        self.assertEqual("ok", status["status"])

        bad = self.cases["rerank.error.duplicate-index"]
        with mock.patch.object(
            state_rag,
            "_remote_json",
            return_value=(bad["response"], {"status": "ok"}),
        ):
            with self.assertRaisesRegex(
                state_rag.StateRagError,
                re.escape(str(bad["error_code"])),
            ):
                state_rag._rerank_call(
                    service,
                    "query",
                    ["a", "b"],
                    2,
                )

    def test_chat_json_fixtures_replay_through_production_decoder(self) -> None:
        good = self.cases["chat-json.ok.delta-v4"]
        decoded = state_rag._decode_chat_completion(
            good["response"],
            require_explicit_stop=True,
        )
        self.assertEqual(
            {
                "schema_version": state_rag.DELTA_V4_SCHEMA,
                "deltas": [],
            },
            decoded,
        )

        bad = self.cases["chat-json.error.invalid-content"]
        with self.assertRaisesRegex(
            state_rag.StateRagError,
            re.escape(str(bad["error_code"])),
        ):
            state_rag._decode_chat_completion(
                bad["response"],
                require_explicit_stop=True,
            )

    def test_chat_tool_fixtures_replay_through_production_decoder(self) -> None:
        good = self.cases["chat-tool.ok.delta-v4"]
        decoded = state_rag._decode_chat_tool_call(
            good["response"],
            expected_tool_name=str(good["tool_name"]),
        )
        self.assertEqual(
            {
                "schema_version": state_rag.DELTA_V4_SCHEMA,
                "deltas": [],
            },
            decoded,
        )

        bad = self.cases["chat-tool.error.wrong-name"]
        with self.assertRaisesRegex(
            state_rag.StateRagError,
            re.escape(str(bad["error_code"])),
        ):
            state_rag._decode_chat_tool_call(
                bad["response"],
                expected_tool_name=str(bad["tool_name"]),
            )


if __name__ == "__main__":
    unittest.main()
