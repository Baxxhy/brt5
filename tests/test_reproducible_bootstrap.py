from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brt5.llm import api_pool
from brt5.scripts.prewarm_swt_environments import (
    attempt_diagnostics,
    failure_diagnostic,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReproducibleBootstrapTests(unittest.TestCase):
    def test_api_pool_json_environment_supports_multiple_keys(self) -> None:
        payload = {
            "apis": [
                {"api_key": "test-key-one", "base_url": "https://one.invalid", "model": "m1"},
                {"api_key": "test-key-two", "base_url": "https://two.invalid", "model": "m2"},
            ]
        }
        with mock.patch.dict(
            os.environ,
            {
                "BRT_API_POOL_JSON": json.dumps(payload),
                "BRT_API_POOL_FILE": "/does/not/exist",
            },
            clear=False,
        ), mock.patch.object(api_pool, "_configured_file", return_value=None):
            entries = api_pool.configured_apis()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0][1:], ("https://one.invalid", "m1"))

    def test_api_pool_file_has_priority_and_is_not_logged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pool.json"
            path.write_text(
                json.dumps(
                    {
                        "apis": [
                            {
                                "api_key": "test-file-key",
                                "base_url": "https://file.invalid",
                                "model": "file-model",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"BRT_API_POOL_FILE": str(path)}, clear=False
            ):
                entries = api_pool.configured_apis()
        self.assertEqual(entries, [("test-file-key", "https://file.invalid", "file-model")])

    def test_api_pool_supports_single_key_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "test-single-key",
                "DEEPSEEK_BASE_URL": "https://single.invalid",
                "DEEPSEEK_MODEL": "single-model",
                "BRT_API_POOL_FILE": "/does/not/exist",
            },
            clear=True,
        ), mock.patch.object(api_pool, "_configured_file", return_value=None):
            entries = api_pool.configured_apis()
        self.assertEqual(
            entries,
            [("test-single-key", "https://single.invalid", "single-model")],
        )

    def test_reproduction_files_exist(self) -> None:
        required = [
            "requirements.txt",
            "requirements-framework.txt",
            "README_REPRODUCE.md",
            ".env.example",
            "config/api_pool.example.json",
            "scripts/bootstrap_machine.sh",
            "scripts/bootstrap_fresh_swt_server.sh",
            "scripts/bootstrap_repositories.py",
            "scripts/configure_api_keys.py",
            "scripts/export_clean_repo.py",
            "scripts/check_repository_secrets.py",
            "scripts/run_swt_experiment.sh",
        ]
        for relative in required:
            self.assertTrue((PROJECT_ROOT / relative).is_file(), relative)

    def test_framework_requirements_exclude_benchmark_environments(self) -> None:
        requirements = {
            line.strip()
            for line in (PROJECT_ROOT / "requirements.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertEqual(
            requirements,
            {
                "datasets==5.0.0",
                "packaging==26.0",
                "requests==2.34.2",
                'tomli>=2.0; python_version < "3.11"',
            },
        )
        self.assertEqual(
            (PROJECT_ROOT / "requirements-framework.txt")
            .read_text(encoding="utf-8")
            .splitlines()[-1],
            "-r requirements.txt",
        )

    def test_launcher_has_no_fixed_workspace_root(self) -> None:
        launcher = (
            PROJECT_ROOT / "scripts" / "run_p0_simple_llm_selector_full.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('PROJECT_ROOT=${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}', launcher)
        self.assertIn('REPO_ROOT=${REPO_ROOT:-$PACKAGE_ROOT/swe_repos}', launcher)
        self.assertNotIn("PROJECT_ROOT=${PROJECT_ROOT:-/root/Baxxhy", launcher)

    def test_fresh_swt_bootstrap_reuses_existing_conda_and_prewarms(self) -> None:
        bootstrap = (
            PROJECT_ROOT / "scripts" / "bootstrap_fresh_swt_server.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("must already be installed", bootstrap)
        self.assertNotIn("Miniconda3-", bootstrap)
        self.assertNotIn("Miniforge3-", bootstrap)
        self.assertIn("prewarm_swt_environments.py", bootstrap)
        self.assertIn("CONDA_ENVS_PATH", bootstrap)
        self.assertIn("CONDA_PKGS_DIRS", bootstrap)
        self.assertIn("template_environment_gate=52/52_ready", bootstrap)
        self.assertIn("validate_behavior_target_cache.py", bootstrap)
        self.assertIn("bootstrap_repositories.py", bootstrap)

    def test_swt_wrapper_loads_generated_runtime_contract(self) -> None:
        wrapper = (
            PROJECT_ROOT / "scripts" / "run_swt_experiment.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(".bootstrap/use_fresh_swt_server.sh", wrapper)
        self.assertIn("--dataset swt", wrapper)
        self.assertIn("run_p0_simple_llm_selector_full.sh", wrapper)

    def test_swt_failure_diagnostic_identifies_shell_redirection(self) -> None:
        diagnostic = failure_diagnostic(
            {
                "status": "CREATE_ERROR",
                "returncode": 1,
                "stderr": (
                    "/tmp/brt3_icore_env_setup.sh: line 6: "
                    "3: No such file or directory"
                ),
            }
        )
        self.assertEqual(diagnostic["category"], "SHELL_REDIRECTION")
        self.assertEqual(diagnostic["returncode"], 1)

    def test_swt_failure_diagnostic_prefers_health_category(self) -> None:
        diagnostic = failure_diagnostic(
            {
                "status": "ENV_HEALTH_ERROR",
                "returncode": 1,
                "health": {
                    "ok": False,
                    "category": "ENV_NOT_FOUND",
                    "reason": "conda environment not found",
                },
                "stderr": "generic failure",
            }
        )
        self.assertEqual(diagnostic["category"], "ENV_NOT_FOUND")
        self.assertEqual(diagnostic["error_line"], "conda environment not found")

    def test_swt_attempt_diagnostics_preserves_initial_root_cause(self) -> None:
        diagnostic = attempt_diagnostics(
            [
                {
                    "attempt": 1,
                    "elapsed_seconds": 7.1,
                    "result": {
                        "status": "CREATE_ERROR",
                        "returncode": 1,
                        "stderr": (
                            "/tmp/brt3_icore_env_setup.sh: line 6: "
                            "3: No such file or directory"
                        ),
                    },
                },
                {
                    "attempt": 2,
                    "elapsed_seconds": 1.0,
                    "result": {
                        "status": "ENV_HEALTH_ERROR",
                        "returncode": 1,
                        "health": {
                            "ok": False,
                            "category": "ENV_NOT_FOUND",
                        },
                    },
                },
            ]
        )
        self.assertEqual(
            diagnostic["root_cause"]["category"], "SHELL_REDIRECTION"
        )
        self.assertEqual(len(diagnostic["attempt_history"]), 2)


if __name__ == "__main__":
    unittest.main()
