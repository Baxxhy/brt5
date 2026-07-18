from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brt5.retrieval import icore_runtime
from brt5.pipeline import run as pipeline_run


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, filename: str):
    path = PROJECT_ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


formal_runner = load_script(
    "brt5_formal_runner_for_test", "run_formal_eval_after_generation.py"
)
tdd_preflight = load_script(
    "brt5_tdd_preflight_for_test", "preflight_tdd_local_conda.py"
)


def tdd_row(instance_id: str = "django__django-7530") -> dict:
    return {
        "instance_id": instance_id,
        "repo": "django/django",
        "version": "1.11",
        "base_commit": "base123",
        "environment_setup_commit": "setup456",
        "patch": "diff --git a/a b/a\n",
    }


class TddLocalCondaContractTests(unittest.TestCase):
    def test_tdd_formal_contract_uses_brt_local_conda(self) -> None:
        contract = formal_runner.validate_runtime_contract(
            "tdd", "local_conda", [tdd_row()]
        )
        self.assertEqual(contract["runtime_backend"], "local_conda")
        self.assertEqual(
            contract["evaluation_module"], "brt5.evaluation.formal_eval"
        )
        self.assertFalse(contract["docker_harness_invoked"])
        self.assertFalse(contract["swebench_lite_fallback_allowed"])
        self.assertTrue(contract["strict_runtime_scrub"])

    def test_tdd_rejects_docker_backend(self) -> None:
        with self.assertRaisesRegex(ValueError, "Docker harness"):
            formal_runner.validate_runtime_contract(
                "tdd", "docker", [tdd_row()]
            )

    def test_generation_tdd_contract_enables_strict_local_conda(self) -> None:
        args = type("Args", (), {
            "dataset_mode": "tdd",
            "runtime_backend": "local_conda",
            "no_conda": False,
        })()
        with mock.patch.dict("os.environ", {}, clear=False):
            contract = pipeline_run.configure_runtime_contract(args)
            self.assertEqual(os.environ["BRT_TDD_STRICT_LOCAL_CONDA"], "1")
        self.assertTrue(contract["strict_runtime_scrub"])
        self.assertFalse(contract["docker_harness_invoked"])

    def test_generation_tdd_contract_rejects_no_conda(self) -> None:
        args = type("Args", (), {
            "dataset_mode": "tdd",
            "runtime_backend": "local_conda",
            "no_conda": True,
        })()
        with self.assertRaisesRegex(ValueError, "--no_conda"):
            pipeline_run.configure_runtime_contract(args)

    def test_generation_swt_contract_does_not_enable_tdd_scrub(self) -> None:
        args = type("Args", (), {
            "dataset_mode": "swt",
            "runtime_backend": "local_conda",
            "no_conda": False,
        })()
        with mock.patch.dict("os.environ", {}, clear=True):
            contract = pipeline_run.configure_runtime_contract(args)
            self.assertNotIn("BRT_TDD_STRICT_LOCAL_CONDA", os.environ)
        self.assertNotIn("strict_runtime_scrub", contract)

    def test_tdd_rejects_missing_gold_patch(self) -> None:
        row = tdd_row()
        row["patch"] = ""
        with self.assertRaisesRegex(ValueError, "golden patch"):
            formal_runner.validate_runtime_contract(
                "tdd", "local_conda", [row]
            )

    def test_swt_contract_keeps_existing_local_evaluator_without_tdd_checks(self) -> None:
        row = tdd_row("django__django-11049")
        row["patch"] = ""
        row["environment_setup_commit"] = ""
        contract = formal_runner.validate_runtime_contract(
            "swt", "local_conda", [row]
        )
        self.assertEqual(
            contract["evaluation_module"], "brt5.evaluation.formal_eval"
        )
        self.assertNotIn("swebench_lite_fallback_allowed", contract)

    def test_preflight_dataset_validation_is_size_agnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tdd.json"
            rows = [tdd_row("one"), tdd_row("two")]
            path.write_text(json.dumps(rows), encoding="utf-8")
            loaded = tdd_preflight.load_rows(path)
        self.assertEqual(len(loaded), 2)

    def test_preflight_rejects_duplicate_instance_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tdd.json"
            path.write_text(
                json.dumps([tdd_row("same"), tdd_row("same")]),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                tdd_preflight.load_rows(path)

    def test_launcher_gates_only_tdd_mode(self) -> None:
        launcher = (
            PROJECT_ROOT / "scripts" / "run_p0_simple_llm_selector_full.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('if [ "$DATASET_MODE" = "tdd" ]; then', launcher)
        self.assertIn("preflight_tdd_local_conda.py", launcher)
        self.assertIn("tdd_local_conda_full.lock", launcher)
        self.assertIn("docker_harness_invoked=false", launcher)
        self.assertNotIn("tddbench.harness.run_evaluation", launcher)

    def test_strict_scrub_script_detects_workspace_paths_inside_pth_code(self) -> None:
        completed = {
            "returncode": 0,
            "stdout": json.dumps({
                "remaining_editable_bindings": [],
                "removed_external_pth": [],
            }),
            "stderr": "",
        }
        with mock.patch.object(
            icore_runtime, "_run_script", return_value=completed
        ) as run_script:
            result = icore_runtime._scrub_cloned_editable_installs(
                "runtime_env", "/tmp", 120
            )
        command = run_script.call_args.args[0]
        self.assertIn("BRT_TDD_STRICT_LOCAL_CONDA", command)
        self.assertIn("BRT_WORKSPACE_ROOT", command)
        self.assertNotIn("/root/Baxxhy/BugReproduce/", command)
        self.assertEqual(result["returncode"], 0)


if __name__ == "__main__":
    unittest.main()
