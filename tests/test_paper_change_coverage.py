from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from brt5.evaluation.direct_eval import (
    SWT_TRACE_PATH,
    aggregate_delta_change_coverage,
    combine_patch_coverage,
    parse_patch_coverage,
    test_command,
    trace_test_command,
)


def _coverage_view(
    executable: dict[str, list[int]],
    hit_counts: dict[str, dict[int, int]],
    *,
    status: str = "OK",
) -> dict:
    return {
        "status": status,
        "executable_lines_by_file": executable,
        "hit_counts_by_file": {
            path: {str(line): count for line, count in counts.items()}
            for path, counts in hit_counts.items()
        },
    }


class PaperChangeCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = {
            "buggy": {"pkg/module.py": [10, 11]},
            "fixed": {"pkg/module.py": [20, 21]},
        }
        self.pred_pre = _coverage_view(
            {"pkg/module.py": [10, 11]},
            {"pkg/module.py": {10: 2, 11: 0}},
        )
        self.pred_post = _coverage_view(
            {"pkg/module.py": [20, 21]},
            {"pkg/module.py": {20: 1, 21: 2}},
        )
        self.gold_pre = _coverage_view(
            {"pkg/module.py": [10, 11]},
            {"pkg/module.py": {10: 2, 11: 1}},
        )
        self.gold_post = _coverage_view(
            {"pkg/module.py": [20, 21]},
            {"pkg/module.py": {20: 1, 21: 1}},
        )
        self.base_pre = _coverage_view(
            {"pkg/module.py": [10, 11]},
            {"pkg/module.py": {10: 0, 11: 0}},
        )
        # The checked-in SWT-Bench implementation uses base_post for both
        # removed and added line deltas.  This fixture locks that compatibility
        # contract instead of silently substituting the semantic base_pre view.
        self.base_post = _coverage_view(
            {"pkg/module.py": [10, 11, 20, 21]},
            {"pkg/module.py": {10: 1, 11: 1, 20: 1, 21: 0}},
        )
        self.gold_base_pre = copy.deepcopy(self.base_pre)
        self.gold_base_post = copy.deepcopy(self.base_post)

    def _combined(self) -> dict:
        return combine_patch_coverage(
            self.targets,
            self.pred_pre,
            self.pred_post,
            self.gold_pre,
            self.gold_post,
            self.base_pre,
            self.base_post,
            self.gold_base_pre,
            self.gold_base_post,
        )

    def test_per_instance_delta_matches_swt_six_view_contract(self) -> None:
        result = self._combined()

        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["paper_metric_eligible"])
        self.assertEqual(result["removed_line_baseline_view"], "base_post")
        self.assertEqual(result["target_line_count"], 4)
        self.assertEqual(result["coverage_pred"], 0.75)
        self.assertEqual(result["coverage_delta_pred"], 0.5)
        self.assertEqual(result["coverage_delta_gold"], 0.5)
        self.assertEqual(
            result["delta_covered_removed_lines"],
            [{"path": "pkg/module.py", "line": 10}],
        )
        self.assertEqual(
            result["delta_covered_added_lines"],
            [{"path": "pkg/module.py", "line": 21}],
        )

    def test_macro_delta_uses_only_gold_applicable_instances(self) -> None:
        first = self._combined()
        second = copy.deepcopy(first)
        second["coverage_delta_pred"] = 0.0
        no_executable = combine_patch_coverage(
            {"buggy": {}, "fixed": {}},
            _coverage_view({}, {}),
            _coverage_view({}, {}),
            _coverage_view({}, {}),
            _coverage_view({}, {}),
            _coverage_view({}, {}),
            _coverage_view({}, {}),
            _coverage_view({}, {}),
            _coverage_view({}, {}),
        )

        aggregate = aggregate_delta_change_coverage(
            {
                "instance-a": {"patch_coverage": first},
                "instance-b": {"patch_coverage": second},
                "instance-no-executable": {"patch_coverage": no_executable},
            },
            coverage_enabled=True,
        )

        self.assertTrue(aggregate["valid"])
        self.assertEqual(aggregate["denominator"], 2)
        self.assertEqual(aggregate["numerator"], 0.5)
        self.assertEqual(aggregate["value"], 0.25)
        self.assertEqual(
            aggregate["excluded_no_executable_ids"],
            ["instance-no-executable"],
        )

    def test_gold_denominator_is_independent_of_model_baseline(self) -> None:
        model_base_post = _coverage_view(
            {"pkg/module.py": [10, 11, 20, 21]},
            {"pkg/module.py": {10: 99, 11: 99, 20: 99, 21: 99}},
        )
        result = combine_patch_coverage(
            self.targets,
            self.pred_pre,
            self.pred_post,
            self.gold_pre,
            self.gold_post,
            self.base_pre,
            model_base_post,
            self.gold_base_pre,
            self.gold_base_post,
        )

        self.assertEqual(result["coverage_delta_pred"], 0.0)
        self.assertEqual(result["coverage_delta_gold"], 0.5)
        self.assertTrue(result["gold_applicable"])
        self.assertEqual(
            result["gold_denominator_source"],
            "independent_gold_test_and_gold_baseline_views",
        )

    def test_missing_gold_applicability_invalidates_paper_metric(self) -> None:
        aggregate = aggregate_delta_change_coverage(
            {
                "instance-a": {"patch_coverage": self._combined()},
                "instance-missing": {
                    "patch_coverage": {
                        "status": "MISSING_GENERATION",
                        "coverage_delta_pred": 0.0,
                    }
                },
            },
            coverage_enabled=True,
        )

        self.assertFalse(aggregate["valid"])
        self.assertIsNone(aggregate["value"])
        self.assertEqual(
            aggregate["invalid_instances"],
            {"instance-missing": "MISSING_GOLD_REFERENCE"},
        )

    def test_coverage_command_can_run_the_complete_generated_file(self) -> None:
        complete_file = test_command(
            "astropy/astropy", "5.0", "tests/test_generated.py", ""
        )
        selected_test = test_command(
            "astropy/astropy",
            "5.0",
            "tests/test_generated.py",
            "GeneratedTests::test_one",
        )

        self.assertIn("tests/test_generated.py", complete_file)
        self.assertNotIn("::", complete_file)
        self.assertIn(
            "tests/test_generated.py::GeneratedTests::test_one", selected_test
        )

    def test_vendored_swt_tracer_captures_python_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            child = root / "child_module.py"
            child.write_text(
                "def child():\n"
                "    value = 1\n"
                "    return value\n"
                "\n"
                "child()\n",
                encoding="utf-8",
            )
            (root / "runner.py").write_text(
                "import subprocess\n"
                "import sys\n"
                "from pathlib import Path\n"
                "subprocess.run([sys.executable, str(Path(__file__).with_name('child_module.py'))], check=True)\n",
                encoding="utf-8",
            )
            coverage_output = root / "coverage.cover"
            command = trace_test_command(
                "python -m runner",
                str(coverage_output),
                str(root),
                {"child_module.py": [2]},
            )
            environment = dict(os.environ)
            environment["PATH"] = (
                str(Path(sys.executable).parent)
                + os.pathsep
                + environment.get("PATH", "")
            )
            environment["PYTHONPATH"] = (
                str(root)
                + os.pathsep
                + environment.get("PYTHONPATH", "")
            )

            completed = subprocess.run(
                command,
                shell=True,
                executable="/bin/bash",
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            parsed = parse_patch_coverage(
                coverage_output,
                {"child_module.py": [2]},
                str(root),
            )

            self.assertTrue(SWT_TRACE_PATH.is_file())
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(parsed["status"], "OK")
            self.assertEqual(parsed["executable_lines_by_file"]["child_module.py"], [2])
            self.assertGreater(
                parsed["hit_counts_by_file"]["child_module.py"]["2"], 0
            )


if __name__ == "__main__":
    unittest.main()
