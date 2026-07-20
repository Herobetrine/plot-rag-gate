from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import state_rag
from scripts.continuity.service import ContinuityService
from scripts.extraction_jobs import (
    ExtractionJobConflict,
    ExtractionJobError,
    ExtractionJobQueue,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _runtime(
    root: Path,
    *,
    shadow: bool = True,
    tool_name: str = state_rag.DEFAULT_EXTRACTION_TOOL_NAME,
) -> state_rag.RuntimeConfig:
    disabled_embedding = state_rag.ServiceConfig(
        name="embedding",
        enabled=False,
        base_url="https://api.siliconflow.cn/v1",
        model="fixture-embedding",
        api_key_env="SILICONFLOW_API_KEY",
        api_key_required=False,
        endpoint="embeddings",
        timeout_seconds=1.0,
    )
    disabled_rerank = state_rag.ServiceConfig(
        name="rerank",
        enabled=False,
        base_url="https://api.siliconflow.cn/v1",
        model="fixture-rerank",
        api_key_env="SILICONFLOW_API_KEY",
        api_key_required=False,
        endpoint="rerank",
        timeout_seconds=1.0,
    )
    extract = state_rag.ServiceConfig(
        name="extract",
        enabled=True,
        base_url="https://api.siliconflow.cn/v1",
        model="fixture-chat",
        api_key_env="SILICONFLOW_API_KEY",
        api_key_required=False,
        endpoint="chat/completions",
        timeout_seconds=1.0,
        max_tokens=2048,
    )
    return state_rag.RuntimeConfig(
        root=root,
        version=3,
        enabled=True,
        db_path=root / ".plot-rag" / "state.sqlite3",
        snapshot_path=root / ".plot-rag" / "state_snapshot.json",
        commit_dir=root / ".plot-rag" / "commits",
        auto_retrieve=True,
        auto_record=True,
        categories=tuple(state_rag.ALLOWED_CATEGORIES),
        top_k=8,
        max_context_chars=12000,
        min_confidence=0.72,
        craft=state_rag.CraftConfig(
            enabled=False,
            auto_retrieve=False,
            use_embedding=False,
            use_rerank=False,
            top_k=4,
            candidate_pool=10,
            max_context_chars=6500,
        ),
        timeout_seconds=1.0,
        embedding=disabled_embedding,
        rerank=disabled_rerank,
        extract=extract,
        extraction_protocol=state_rag.ExtractionProtocolConfig(
            tool_schema_shadow=shadow,
            tool_name=tool_name,
        ),
    )


def _envelope() -> dict[str, object]:
    return {
        "schema_version": state_rag.DELTA_V4_SCHEMA,
        "deltas": [
            {
                "event_type": "state",
                "action": "set",
                "subject": "测试角色甲",
                "object": None,
                "field": "injury",
                "value": "稳定",
                "scope": "current",
                "knowledge_plane": "objective",
                "confidence": 0.99,
                "evidence": "测试角色甲伤势稳定。",
            }
        ],
    }


def _json_response(envelope: object) -> dict[str, object]:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        envelope,
                        ensure_ascii=False,
                    )
                },
            }
        ]
    }


def _tool_response(
    envelope: object,
    *,
    tool_name: str = state_rag.DEFAULT_EXTRACTION_TOOL_NAME,
) -> dict[str, object]:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(
                                    envelope,
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
            }
        ]
    }


class ExtractionToolShadowTests(unittest.TestCase):
    def test_authoritative_json_object_is_unchanged_and_tool_shadow_matches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = _runtime(Path(temporary))
            calls: list[dict[str, object]] = []

            def remote(_service, payload):
                calls.append(dict(payload))
                response = (
                    _json_response(_envelope())
                    if len(calls) == 1
                    else _tool_response(_envelope())
                )
                return response, {
                    "status": "ok",
                    "http_status": 200,
                    "latency_ms": 1,
                }

            with mock.patch.object(
                state_rag,
                "_remote_json",
                side_effect=remote,
            ):
                deltas, skipped, status = state_rag._chat_extract(
                    runtime,
                    "测试角色甲伤势稳定。",
                    "记录明确发生的状态变化。",
                    [],
                )

        self.assertEqual(1, len(deltas))
        self.assertEqual([], skipped)
        self.assertEqual(2, len(calls))
        self.assertEqual(
            {"type": "json_object"},
            calls[0]["response_format"],
        )
        self.assertNotIn("tools", calls[0])
        self.assertNotIn("response_format", calls[1])
        self.assertEqual(
            state_rag.DEFAULT_EXTRACTION_TOOL_NAME,
            calls[1]["tools"][0]["function"]["name"],
        )
        self.assertEqual("equivalent", status["tool_shadow"]["status"])
        self.assertEqual(
            "passed",
            status["tool_shadow"]["validator_status"],
        )
        self.assertTrue(status["tool_shadow"]["equivalent"])
        self.assertFalse(status["tool_shadow"]["acceptance_eligible"])

    def test_shadow_mismatch_and_validator_failure_never_replace_authority(
        self,
    ) -> None:
        variants = (
            (_tool_response({"schema_version": state_rag.DELTA_V4_SCHEMA, "deltas": []}), "mismatch"),
            (
                _tool_response(
                    {
                        "schema_version": state_rag.DELTA_V4_SCHEMA,
                        "deltas": [
                            {
                                **_envelope()["deltas"][0],
                                "evidence": "不存在的证据",
                            }
                        ],
                    }
                ),
                "failed",
            ),
        )
        for tool_response, expected_status in variants:
            with self.subTest(status=expected_status):
                with tempfile.TemporaryDirectory() as temporary:
                    runtime = _runtime(Path(temporary))
                    with mock.patch.object(
                        state_rag,
                        "_remote_json",
                        side_effect=[
                            (
                                _json_response(_envelope()),
                                {"status": "ok"},
                            ),
                            (tool_response, {"status": "ok"}),
                        ],
                    ):
                        deltas, _skipped, status = (
                            state_rag._chat_extract(
                                runtime,
                                "测试角色甲伤势稳定。",
                                "记录明确发生的状态变化。",
                                [],
                            )
                        )

                self.assertEqual("稳定", deltas[0]["value"])
                self.assertEqual(
                    expected_status,
                    status["tool_shadow"]["status"],
                )
                self.assertFalse(
                    status["tool_shadow"]["acceptance_eligible"]
                )

    def test_tool_decoder_rejects_wrong_name_and_invalid_arguments(self) -> None:
        valid = _tool_response(_envelope())
        self.assertEqual(
            _envelope(),
            state_rag._decode_chat_tool_call(
                valid,
                expected_tool_name=state_rag.DEFAULT_EXTRACTION_TOOL_NAME,
            ),
        )
        for invalid in (
            _tool_response(_envelope(), tool_name="wrong_tool"),
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": state_rag.DEFAULT_EXTRACTION_TOOL_NAME,
                                        "arguments": "{",
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        ):
            with self.subTest(response=invalid):
                with self.assertRaises(state_rag.StateRagError):
                    state_rag._decode_chat_tool_call(
                        invalid,
                        expected_tool_name=(
                            state_rag.DEFAULT_EXTRACTION_TOOL_NAME
                        ),
                    )

    def test_protocol_identity_binds_tool_name_and_schema_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = state_rag._extraction_generation_params(
                _runtime(root, tool_name="submit_plot_rag_deltas")
            )
            second = state_rag._extraction_generation_params(
                _runtime(root, tool_name="submit_plot_rag_deltas_v2")
            )

        self.assertEqual("json_object", first["authoritative_protocol"])
        self.assertTrue(first["tool_shadow"]["enabled"])
        self.assertEqual(
            "tool_function_arguments",
            first["tool_shadow"]["protocol"],
        )
        self.assertRegex(
            first["tool_shadow"]["schema_hash"],
            r"^[0-9a-f]{64}$",
        )
        self.assertNotEqual(
            state_rag._sha256_json(first),
            state_rag._sha256_json(second),
        )

    def test_checked_in_config_loads_shadow_policy_and_rejects_override(
        self,
    ) -> None:
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "config.v3.json"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_dir = root / ".plot-rag"
            config_dir.mkdir()
            config = json.loads(template.read_text(encoding="utf-8"))
            config["extraction_protocol"]["tool_schema_shadow"] = True
            (config_dir / "config.json").write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )
            loaded = state_rag._load_runtime_config(root)
            self.assertTrue(
                loaded.extraction_protocol.tool_schema_shadow
            )
            config["extraction_protocol"][
                "authoritative_protocol"
            ] = "tool_only"
            (config_dir / "config.json").write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                state_rag.StateRagError,
                "must remain json_object",
            ):
                state_rag._load_runtime_config(root)


class ExtractionJobProtocolIdentityTests(unittest.TestCase):
    def test_job_hash_binds_protocol_tool_name_and_schema_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replay = ContinuityService(root).replay()
            queue = ExtractionJobQueue(root)
            prompt_hash = _digest("prompt")
            with queue.store.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO turns(
                        receipt_id, request_id, prompt, prompt_hash,
                        assistant_hash, status, started_at
                    ) VALUES(?, ?, '', ?, '', 'pending', ?)
                    """,
                    (
                        "receipt-protocol",
                        "request-protocol",
                        prompt_hash,
                        "2026-07-17T00:00:00Z",
                    ),
                )
            runtime = _runtime(root)
            base = {
                "receipt_id": "receipt-protocol",
                "request_id": "request-protocol",
                "assistant_text": "测试角色甲伤势稳定。",
                "prompt_hash": prompt_hash,
                "retrieved_context_digest": _digest("context"),
                "prepared_canon_revision": int(
                    replay["active_canon_revision"]
                ),
                "active_projection_hash": str(
                    replay["projection_hash"]
                ),
                "artifact_context": {
                    "artifact_id": "chapter-1",
                    "artifact_stage": "draft",
                },
                "branch_id": "main",
                "sequence_no": 1,
                "extract_provider": "extract",
                "extract_base_url": "https://api.siliconflow.cn/v1",
                "extract_model": "fixture-chat",
                "extract_schema_hash": _digest("schema"),
                "extract_prompt_template_hash": _digest("template"),
                "min_confidence": 0.72,
            }
            first = queue.enqueue(
                **base,
                generation_params=(
                    state_rag._extraction_generation_params(runtime)
                ),
            )
            self.assertEqual(
                state_rag.DEFAULT_EXTRACTION_TOOL_NAME,
                first["generation_params"]["tool_shadow"]["tool_name"],
            )
            self.assertRegex(
                first["generation_params_hash"],
                r"^[0-9a-f]{64}$",
            )
            changed = state_rag._extraction_generation_params(
                _runtime(root, tool_name="submit_plot_rag_deltas_v2")
            )
            with self.assertRaisesRegex(
                ExtractionJobConflict,
                "different extraction inputs",
            ):
                queue.enqueue(**base, generation_params=changed)

    def test_job_queue_rejects_acceptance_eligible_shadow(self) -> None:
        generation = {
            "temperature": 0,
            "max_tokens": 1024,
            "authoritative_protocol": "json_object",
            "tool_shadow": {
                "enabled": True,
                "protocol": "tool_function_arguments",
                "tool_name": state_rag.DEFAULT_EXTRACTION_TOOL_NAME,
                "schema_hash": _digest("tool-schema"),
                "acceptance_eligible": True,
            },
        }
        with self.assertRaisesRegex(
            ExtractionJobError,
            "diagnostic-only",
        ):
            from scripts.extraction_jobs import _normalize_generation_params

            _normalize_generation_params(generation)


if __name__ == "__main__":
    unittest.main()
