from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import performance_runtime as runtime  # noqa: E402


class PerformancePortabilityTests(unittest.TestCase):
    def test_windows_sqlite_uri_preserves_extended_and_unc_paths(self) -> None:
        local = runtime._windows_sqlite_readonly_uri(
            r"\\?\C:\作品\state.sqlite3"
        )
        unc = runtime._windows_sqlite_readonly_uri(
            r"\\?\UNC\HOST\SHARE\作品\state.sqlite3"
        )

        self.assertEqual(
            "file:C%3A%5C%E4%BD%9C%E5%93%81%5Cstate.sqlite3?mode=ro",
            local,
        )
        self.assertTrue(unc.startswith("file:%5C%5CHOST%5CSHARE%5C"))
        self.assertNotIn("file://HOST", unc)
        self.assertTrue(unc.endswith("?mode=ro"))

    @unittest.skipUnless(
        os.name == "nt",
        "Windows extended-length paths are Windows-specific",
    )
    def test_open_readonly_accepts_windows_extended_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE sample(value INTEGER)")
                connection.execute("INSERT INTO sample VALUES(7)")
                connection.commit()

            extended = Path("\\\\?\\" + str(database))
            with closing(runtime._open_readonly(extended)) as connection:
                value = connection.execute(
                    "SELECT value FROM sample"
                ).fetchone()[0]
                query_only = connection.execute(
                    "PRAGMA query_only"
                ).fetchone()[0]

        self.assertEqual(7, value)
        self.assertEqual(1, query_only)

    def test_runtime_accepts_utf8_bom_manifest_path(self) -> None:
        fixture = (
            PLUGIN_ROOT
            / "benchmarks"
            / "fixtures"
            / "v15_performance_manifest.v1.json"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            manifest = root / "manifest.json"
            manifest.write_text(
                fixture.read_text(encoding="utf-8"),
                encoding="utf-8-sig",
            )

            result = runtime.run_benchmark(
                project,
                manifest,
                options={
                    "iterations": 1,
                    "rerank_delay_ms": 0,
                },
            )

        self.assertTrue(result["passed"])
        self.assertEqual("passed", result["status"])

    def test_runtime_loader_ignores_foreign_benchmarks_and_longform(
        self,
    ) -> None:
        runtime_path = PLUGIN_ROOT / "scripts" / "performance_runtime.py"
        script = f"""
import importlib.util
import pathlib
import sys
import tempfile

root = pathlib.Path({str(PLUGIN_ROOT)!r})
with tempfile.TemporaryDirectory() as temporary:
    foreign = pathlib.Path(temporary)
    for package in ("benchmarks", "longform"):
        path = foreign / package
        path.mkdir()
        (path / "__init__.py").write_text(
            "FOREIGN_SENTINEL = True\\n",
            encoding="utf-8",
        )
    sys.path.insert(0, str(foreign))
    import benchmarks
    import longform

    spec = importlib.util.spec_from_file_location(
        "_performance_runtime_portability_test",
        {str(runtime_path)!r},
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    before = list(sys.path)
    benchmark = module._load_benchmark_module()
    validation = benchmark.validate_fixture_manifest(
        benchmark.default_fixture_manifest()
    )
    assert validation["status"] == "valid"
    assert benchmarks.FOREIGN_SENTINEL
    assert longform.FOREIGN_SENTINEL
    assert sys.path == before
    assert benchmark.AuthorityIndex.__module__.startswith(
        "_plot_rag_gate_benchmark_"
    )
"""
        completed = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", "-c", script],
            cwd=PLUGIN_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_cli_accepts_bom_with_foreign_top_level_packages(self) -> None:
        runner = (
            PLUGIN_ROOT
            / "benchmarks"
            / "run_v15_performance_benchmark.py"
        )
        fixture = (
            PLUGIN_ROOT
            / "benchmarks"
            / "fixtures"
            / "v15_performance_manifest.v1.json"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.json"
            manifest.write_text(
                fixture.read_text(encoding="utf-8"),
                encoding="utf-8-sig",
            )
            foreign = root / "foreign"
            for package in ("benchmarks", "longform"):
                package_root = foreign / package
                package_root.mkdir(parents=True)
                (package_root / "__init__.py").write_text(
                    "FOREIGN_SENTINEL = True\n",
                    encoding="utf-8",
                )
            script = f"""
import runpy
import sys

sys.path.insert(0, {str(foreign)!r})
import benchmarks
import longform
sys.argv = [
    {str(runner)!r},
    "validate",
    "--manifest",
    {str(manifest)!r},
]
try:
    runpy.run_path({str(runner)!r}, run_name="__main__")
except SystemExit as error:
    if error.code not in (None, 0):
        raise
assert benchmarks.FOREIGN_SENTINEL
assert longform.FOREIGN_SENTINEL
"""
            completed = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", "-c", script],
                cwd=PLUGIN_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("valid", json.loads(completed.stdout)["status"])


if __name__ == "__main__":
    unittest.main()
