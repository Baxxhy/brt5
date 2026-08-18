from __future__ import annotations

import ast
from pathlib import Path
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_swt_official_env_compat.py"


class OfficialEnvironmentCompatTests(unittest.TestCase):
    def test_script_is_valid_python(self) -> None:
        ast.parse(SCRIPT.read_text(encoding="utf-8"))

    def test_matplotlib_uses_available_template_and_writes_setup_cfg(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("config_template=setup.cfg.template", source)
        self.assertIn("[ -f mplsetup.cfg.template ]", source)
        self.assertIn('cp \\"$config_template\\" setup.cfg', source)
        self.assertIn("system_freetype = True", source)
        self.assertIn("system_qhull = True", source)

    def test_sympy_py_directive_repair_is_narrow(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('self.repo == "sympy/sympy" and self.compute_coverage', source)
        self.assertIn('directive.endswith(".py")', source)
        self.assertIn("directive[:-1]", source)

    def test_raw_github_fetch_prefers_local_immutable_git_object(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"git", "-C", str(local_repo), "show"', source)
        self.assertIn("status_forcelist=(429, 500, 502, 503, 504)", source)
        self.assertIn("install_retryable_source_fetches()", source)

    def test_utf8_compat_patches_both_official_module_names(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('(\"run_evaluation\", \"src.run_evaluation\")', source)
        self.assertIn('decode(\"utf-8\", errors=\"replace\")', source)

    def test_quiet_eval_only_removes_diagnostics_and_optional_reinstall(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('{\"git status\", \"git show\", f\"git diff {self.base_commit}\"}', source)
        self.assertIn("if skip_reinstall:", source)
        self.assertIn("install_quiet_eval_diagnostics(skip_reinstall=False)", source)
        self.assertIn('container.exec_run("git diff", workdir="/testbed")', source)
        self.assertIn('logger.info(f"{annotation} bytes={len(output)} sha256={digest}")', source)
        self.assertIn("return output", source)

    def test_astropy_eval_reinstall_reapplies_build_setuptools_pin(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('f"{pin_build_setuptools} && "', source)
        self.assertIn("setuptools==59.8.0", source)


if __name__ == "__main__":
    unittest.main()
