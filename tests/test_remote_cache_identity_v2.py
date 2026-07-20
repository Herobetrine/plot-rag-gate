from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from plot_init.canonical import (  # noqa: E402
    canonical_hash,
    canonical_json,
    sha256_text,
    utc_now,
)
from plot_init.remote_cache import (  # noqa: E402
    REMOTE_CACHE_IDENTITY_PROTOCOL,
    REMOTE_CACHE_PROTOCOL,
    MemoryRemoteResponseCache,
    RemoteCacheIdentity,
    SQLiteRemoteResponseCache,
)
from plot_init.remote_model import (  # noqa: E402
    RemoteModelConfig,
    _chat_url,
    _effective_generation_parameters,
    _remote_json,
    load_remote_model_config,
    resolve_classification_review,
)
import plot_init.remote_cache as remote_cache_module  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._raw


class _RecordingOpener:
    def __init__(self) -> None:
        self.request: Any = None
        self.timeout: float | None = None

    def open(self, request: Any, *, timeout: float) -> _FakeResponse:
        self.request = request
        self.timeout = timeout
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"accepted": True},
                                separators=(",", ":"),
                            )
                        }
                    }
                ]
            }
        )


class RemoteCacheIdentityV2Tests(unittest.TestCase):
    def identity(self, **overrides: Any) -> RemoteCacheIdentity:
        values: dict[str, Any] = {
            "provider": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "fixture-model",
            "prompt": {"task": "classification", "source": "alpha"},
            "system_prompt": "system contract v1",
            "schema": {"type": "object"},
            "source_hash": "source",
            "generation_parameters": {
                "temperature": 0.0,
                "top_p": 1.0,
                "max_tokens": 2400,
                "response_format": {"type": "json_object"},
            },
        }
        values.update(overrides)
        return RemoteCacheIdentity.build(**values)

    def test_identity_binds_provider_endpoint_prompt_and_generation(self) -> None:
        baseline = self.identity()
        equivalent = self.identity(
            provider="SILICONFLOW",
            base_url="HTTPS://API.SILICONFLOW.CN.:443/v1/",
        )
        endpoint_equivalent = self.identity(
            base_url="https://api.siliconflow.cn/v1/chat/completions",
        )

        self.assertEqual(REMOTE_CACHE_IDENTITY_PROTOCOL, baseline.identity_protocol)
        self.assertEqual("siliconflow", baseline.provider)
        self.assertEqual("https://api.siliconflow.cn/v1", baseline.base_url)
        self.assertEqual(baseline.cache_key, equivalent.cache_key)
        self.assertEqual(baseline.cache_key, endpoint_equivalent.cache_key)
        self.assertRegex(baseline.system_prompt_hash, r"^[0-9a-f]{64}$")
        self.assertRegex(
            baseline.generation_parameters_hash,
            r"^[0-9a-f]{64}$",
        )

        variants = {
            "provider": {"provider": "openai-compatible"},
            "base_url": {"base_url": "https://proxy.example/v1"},
            "system_prompt": {"system_prompt": "system contract v2"},
            "temperature": {
                "generation_parameters": {
                    "temperature": 0.1,
                    "top_p": 1.0,
                    "max_tokens": 2400,
                    "response_format": {"type": "json_object"},
                }
            },
            "top_p": {
                "generation_parameters": {
                    "temperature": 0.0,
                    "top_p": 0.9,
                    "max_tokens": 2400,
                    "response_format": {"type": "json_object"},
                }
            },
            "max_tokens": {
                "generation_parameters": {
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "max_tokens": 1200,
                    "response_format": {"type": "json_object"},
                }
            },
            "response_format": {
                "generation_parameters": {
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "max_tokens": 2400,
                    "response_format": {"type": "text"},
                }
            },
        }
        for dimension, overrides in variants.items():
            with self.subTest(dimension=dimension):
                self.assertNotEqual(
                    baseline.cache_key,
                    self.identity(**overrides).cache_key,
                )

    def test_v1_sqlite_row_is_an_explicit_miss_for_v2_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "init.sqlite3"
            cache = SQLiteRemoteResponseCache(database_path)
            identity = self.identity()
            prompt = {"task": "classification", "source": "alpha"}
            schema = {"type": "object"}
            legacy_key = sha256_text(
                canonical_json(
                    {
                        "model": "fixture-model",
                        "prompt_hash": canonical_hash(prompt),
                        "schema_hash": canonical_hash(schema),
                        "source_hash": sha256_text("source"),
                    }
                )
            )
            cache._initialize()
            with closing(sqlite3.connect(database_path)) as connection:
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO initialization_remote_response_cache(
                        cache_key, model, prompt_hash, schema_hash, source_hash,
                        response_json, response_hash, created_at, accessed_at,
                        hit_count
                    ) VALUES(?,?,?,?,?,?,?,?,?,0)
                    """,
                    (
                        legacy_key,
                        identity.model,
                        identity.prompt_hash,
                        identity.schema_hash,
                        identity.source_hash,
                        canonical_json({"value": "legacy-v1"}),
                        canonical_hash({"value": "legacy-v1"}),
                        now,
                        now,
                    ),
                )
                connection.commit()

            loader_calls = 0

            def loader() -> dict[str, str]:
                nonlocal loader_calls
                loader_calls += 1
                return {"value": "fresh-v2"}

            resolved = cache.resolve(
                provider=identity.provider,
                base_url=identity.base_url,
                model=identity.model,
                prompt=prompt,
                system_prompt="system contract v1",
                schema=schema,
                source_hash="source",
                generation_parameters={
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "max_tokens": 2400,
                    "response_format": {"type": "json_object"},
                },
                loader=loader,
            )

            self.assertEqual(REMOTE_CACHE_PROTOCOL, resolved["protocol"])
            self.assertFalse(resolved["cache_hit"])
            self.assertEqual({"value": "fresh-v2"}, resolved["response"])
            self.assertEqual(1, loader_calls)
            with closing(sqlite3.connect(database_path)) as connection:
                keys = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT cache_key FROM initialization_remote_response_cache"
                    )
                }
            self.assertEqual({legacy_key, identity.cache_key}, keys)

    def test_cached_response_hash_is_enforced_before_memory_hit(self) -> None:
        identity = self.identity()
        mutations = {
            "response_tamper": lambda entry: entry.__setitem__(
                "response",
                {"value": "tampered"},
            ),
            "invalid_hash": lambda entry: entry.__setitem__(
                "response_hash",
                "not-a-sha256",
            ),
        }
        for case, mutate in mutations.items():
            with self.subTest(case=case):
                cache = MemoryRemoteResponseCache()
                cache.put(identity, {"value": "trusted"})
                mutate(cache._entries[identity.cache_key])

                self.assertIsNone(cache.get(identity))
                self.assertNotIn(identity.cache_key, cache._entries)

    def test_sqlite_cache_evicts_invalid_json_or_response_hash(self) -> None:
        identity = self.identity()
        cases = {
            "response_tamper": (
                canonical_json({"value": "tampered"}),
                canonical_hash({"value": "trusted"}),
            ),
            "invalid_json": (
                "{",
                canonical_hash({"value": "trusted"}),
            ),
            "invalid_hash": (
                canonical_json({"value": "trusted"}),
                "not-a-sha256",
            ),
        }
        for case, (response_json, response_hash) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                database_path = Path(temporary) / "cache.sqlite3"
                cache = SQLiteRemoteResponseCache(database_path)
                cache.put(identity, {"value": "trusted"})
                with closing(sqlite3.connect(database_path)) as connection:
                    connection.execute(
                        """
                        UPDATE initialization_remote_response_cache
                        SET response_json=?, response_hash=?
                        WHERE cache_key=?
                        """,
                        (
                            response_json,
                            response_hash,
                            identity.cache_key,
                        ),
                    )
                    connection.commit()

                self.assertIsNone(cache.get(identity))
                with closing(sqlite3.connect(database_path)) as connection:
                    row_count = connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM initialization_remote_response_cache
                        WHERE cache_key=?
                        """,
                        (identity.cache_key,),
                    ).fetchone()[0]
                self.assertEqual(0, row_count)

    def test_expired_reader_does_not_delete_concurrent_refresh(self) -> None:
        identity = self.identity()
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "cache.sqlite3"
            cache = SQLiteRemoteResponseCache(
                database_path,
                max_age_seconds=60,
            )
            trusted = {"value": "trusted"}
            cache.put(identity, trusted)
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """
                    UPDATE initialization_remote_response_cache
                    SET created_at='2000-01-01T00:00:00.000000+00:00',
                        accessed_at='2000-01-01T00:00:00.000000+00:00'
                    WHERE cache_key=?
                    """,
                    (identity.cache_key,),
                )
                connection.commit()

            original_connect = sqlite3.connect
            refreshed = False

            class InterceptConnection:
                def __init__(self, inner: sqlite3.Connection) -> None:
                    object.__setattr__(self, "_inner", inner)

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._inner, name)

                def __setattr__(self, name: str, value: Any) -> None:
                    if name == "_inner":
                        object.__setattr__(self, name, value)
                    else:
                        setattr(self._inner, name, value)

                def execute(
                    self,
                    sql: str,
                    parameters: Any = (),
                ) -> sqlite3.Cursor:
                    nonlocal refreshed
                    normalized = " ".join(sql.split())
                    if (
                        not refreshed
                        and normalized.startswith(
                            "DELETE FROM "
                            "initialization_remote_response_cache"
                        )
                    ):
                        refreshed = True
                        now = utc_now()
                        with closing(
                            original_connect(database_path)
                        ) as updater:
                            updater.execute(
                                """
                                UPDATE initialization_remote_response_cache
                                SET created_at=?, accessed_at=?
                                WHERE cache_key=?
                                """,
                                (now, now, identity.cache_key),
                            )
                            updater.commit()
                    return self._inner.execute(sql, parameters)

            def intercept_connect(*args: Any, **kwargs: Any) -> Any:
                return InterceptConnection(
                    original_connect(*args, **kwargs)
                )

            with patch.object(
                remote_cache_module.sqlite3,
                "connect",
                side_effect=intercept_connect,
            ):
                self.assertIsNone(cache.get(identity))

            self.assertTrue(refreshed)
            self.assertEqual(trusted, cache.get(identity))

    @unittest.skipUnless(
        os.name == "posix",
        "case-distinct SQLite paths require a POSIX platform",
    )
    def test_sqlite_singleflight_scope_preserves_case_distinct_posix_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            upper_path = Path(temporary) / "Cache.sqlite3"
            lower_path = Path(temporary) / "cache.sqlite3"
            upper_path.touch()
            lower_path.touch()
            if os.path.samefile(upper_path, lower_path):
                self.skipTest("filesystem is case-insensitive")
            upper_path.unlink()
            lower_path.unlink()

            upper_cache = SQLiteRemoteResponseCache(upper_path)
            lower_cache = SQLiteRemoteResponseCache(lower_path)
            first_started = threading.Event()
            release_first = threading.Event()
            second_started = threading.Event()
            calls = {"upper": 0, "lower": 0}

            def upper_loader() -> dict[str, str]:
                calls["upper"] += 1
                first_started.set()
                if not release_first.wait(timeout=5):
                    raise TimeoutError("upper cache loader was not released")
                return {"database": "upper"}

            def lower_loader() -> dict[str, str]:
                calls["lower"] += 1
                second_started.set()
                return {"database": "lower"}

            resolve_args = {
                "provider": "siliconflow",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "fixture-model",
                "prompt": {"task": "classification", "source": "alpha"},
                "system_prompt": "system contract v1",
                "schema": {"type": "object"},
                "source_hash": "source",
                "generation_parameters": {
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "max_tokens": 2400,
                    "response_format": {"type": "json_object"},
                },
            }
            with ThreadPoolExecutor(max_workers=2) as executor:
                upper_future = executor.submit(
                    upper_cache.resolve,
                    **resolve_args,
                    loader=upper_loader,
                )
                self.assertTrue(first_started.wait(timeout=5))
                lower_future = executor.submit(
                    lower_cache.resolve,
                    **resolve_args,
                    loader=lower_loader,
                )
                lower_started_while_upper_blocked = second_started.wait(timeout=2)
                release_first.set()
                upper_result = upper_future.result(timeout=5)
                lower_result = lower_future.result(timeout=5)

            self.assertTrue(lower_started_while_upper_blocked)
            self.assertEqual({"upper": 1, "lower": 1}, calls)
            self.assertFalse(upper_result["cache_hit"])
            self.assertFalse(lower_result["cache_hit"])
            self.assertEqual(
                {"database": "upper"},
                upper_result["response"],
            )
            self.assertEqual(
                {"database": "lower"},
                lower_result["response"],
            )
            identity = self.identity()
            self.assertEqual(
                {"database": "upper"},
                upper_cache.get(identity),
            )
            self.assertEqual(
                {"database": "lower"},
                lower_cache.get(identity),
            )

    def test_request_payload_and_diagnostics_share_effective_identity(self) -> None:
        config = RemoteModelConfig(
            enabled=True,
            provider="siliconflow",
            base_url="https://api.siliconflow.cn/v1",
            model="fixture-model",
            api_key="TOKEN_TEST_ONLY",
            timeout_seconds=2.0,
            host="api.siliconflow.cn",
            ready=True,
        )
        opener = _RecordingOpener()
        with patch(
            "plot_init.remote_model.urllib.request.build_opener",
            return_value=opener,
        ):
            self.assertEqual(
                {"accepted": True},
                _remote_json(
                    config,
                    system_prompt="system contract v1",
                    user_payload={"source": "alpha"},
                ),
            )

        request_payload = json.loads(opener.request.data.decode("utf-8"))
        for key, value in _effective_generation_parameters().items():
            self.assertEqual(value, request_payload[key])
        self.assertEqual("fixture-model", request_payload["model"])
        self.assertEqual(
            "https://api.siliconflow.cn/v1/chat/completions",
            opener.request.full_url,
        )

        source_text = "这是一份边界模糊的资料。"
        remote_value = {
            "source_role": "setting",
            "confidence": 0.8,
            "exact_evidence": source_text,
        }
        with tempfile.TemporaryDirectory() as temporary:
            cache = SQLiteRemoteResponseCache(
                Path(temporary) / "cache.sqlite3"
            )
            with (
                patch(
                    "plot_init.remote_model.load_remote_model_config",
                    return_value=config,
                ),
                patch(
                    "plot_init.remote_model._remote_json",
                    return_value=remote_value,
                ),
            ):
                first = resolve_classification_review(
                    path="source.md",
                    source_text=source_text,
                    source_hash=sha256_text(source_text),
                    local_classification={
                        "source_role": "note",
                        "classification_confidence": 0.4,
                        "classification_basis": "ambiguous",
                    },
                    remote_cache=cache,
                )
                second = resolve_classification_review(
                    path="source.md",
                    source_text=source_text,
                    source_hash=sha256_text(source_text),
                    local_classification={
                        "source_role": "note",
                        "classification_confidence": 0.4,
                        "classification_basis": "ambiguous",
                    },
                    remote_cache=cache,
                )

        diagnostics = first["diagnostics"]
        self.assertEqual("siliconflow", diagnostics["provider"])
        self.assertEqual(config.base_url, diagnostics["base_url"])
        self.assertEqual(
            REMOTE_CACHE_IDENTITY_PROTOCOL,
            diagnostics["cache_identity_protocol"],
        )
        self.assertRegex(diagnostics["cache_key"], r"^[0-9a-f]{64}$")
        self.assertRegex(diagnostics["system_prompt_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            _effective_generation_parameters(),
            diagnostics["generation_parameters"],
        )
        self.assertRegex(
            diagnostics["generation_parameters_hash"],
            r"^[0-9a-f]{64}$",
        )
        self.assertFalse(diagnostics["cache_hit"])
        self.assertTrue(second["diagnostics"]["cache_hit"])
        self.assertEqual(
            diagnostics["cache_key"],
            second["diagnostics"]["cache_key"],
        )
        self.assertNotIn(
            config.api_key,
            json.dumps(diagnostics, ensure_ascii=False),
        )

    def test_remote_config_normalizes_provider_base_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PLOT_RAG_INIT_REMOTE_ENABLED": "true",
                "PLOT_RAG_LLM_BASE_URL": (
                    "HTTPS://API.SILICONFLOW.CN.:443/v1/chat/completions/"
                ),
                "PLOT_RAG_LLM_MODEL": "fixture-model",
                "SILICONFLOW_API_KEY": "TOKEN_TEST_ONLY",
            },
            clear=True,
        ):
            config = load_remote_model_config()

        self.assertTrue(config.ready)
        self.assertEqual("siliconflow", config.provider)
        self.assertEqual("https://api.siliconflow.cn/v1", config.base_url)
        self.assertEqual(
            "https://api.siliconflow.cn/v1/chat/completions",
            _chat_url(config),
        )


if __name__ == "__main__":
    unittest.main()
