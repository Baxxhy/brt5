from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
for value in (
    ROOT.parent,
    ROOT / "scripts",
    ROOT.parent / "swt-bench",
    ROOT.parent / "swt-bench" / "src",
):
    sys.path.insert(0, str(value))

from revalidate_swt_checkpoints import classify_test_output  # noqa: E402


class StructuredClassificationTests(unittest.TestCase):
    def classify(self, text: str, parsed: dict[str, str] | None = None):
        with TemporaryDirectory(dir="/root") as temporary:
            output = Path(temporary) / "test_output.txt"
            output.write_text(text, encoding="utf-8")
            with patch(
                "revalidate_swt_checkpoints.get_logs_eval",
                return_value=(parsed or {}, True),
            ):
                return classify_test_output(output, "owner/repo")

    def test_pass(self):
        result = self.classify("collected 1 item\n1 passed\n", {"test_x": "PASSED"})
        self.assertEqual(result["category"], "pass")
        self.assertEqual(result["executed_count"], 1)

    def test_assertion_failure(self):
        result = self.classify(
            "collected 1 item\nE assert actual == expected\n1 failed\n",
            {"test_x": "FAILED"},
        )
        self.assertEqual(result["category"], "assertion_failure")

    def test_import_error_before_execution(self):
        result = self.classify("ImportError while importing test module\nImportError: missing\n")
        self.assertEqual(result["category"], "import_error")

    def test_collection_error(self):
        result = self.classify("ERROR collecting tests/test_x.py\n")
        self.assertEqual(result["category"], "collection_error")

    def test_setup_error(self):
        result = self.classify("_____ ERROR at setup of test_x _____\nfixture 'db' not found\n")
        self.assertEqual(result["category"], "setup_error")

    def test_zero_test(self):
        result = self.classify("collected 0 items\nno tests ran\n")
        self.assertEqual(result["category"], "zero_test")

    def test_syntax_error_before_execution(self):
        result = self.classify("SyntaxError: invalid syntax\n")
        self.assertEqual(result["category"], "syntax_error")

    def test_runtime_error(self):
        result = self.classify(
            "collected 1 item\nValueError: boom\n1 failed\n",
            {"test_x": "FAILED"},
        )
        self.assertEqual(result["category"], "runtime_error")

    def test_timeout(self):
        result = self.classify("Tests timed out after 900 seconds\n")
        self.assertEqual(result["category"], "timeout")

    def test_literal_syntaxerror_in_executed_suite_is_not_setup(self):
        result = self.classify(
            "assert result == 'SyntaxError'\ncollected 2 items\n1 passed, 1 failed\n",
            {"test_a": "PASSED", "test_b": "FAILED"},
        )
        self.assertEqual(result["category"], "runtime_error")


if __name__ == "__main__":
    unittest.main()
