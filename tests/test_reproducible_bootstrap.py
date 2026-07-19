from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brt5.llm import api_pool


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
            "scripts/bootstrap_repositories.py",
            "scripts/configure_api_keys.py",
            "scripts/export_clean_repo.py",
            "scripts/check_repository_secrets.py",
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


if __name__ == "__main__":
    unittest.main()
