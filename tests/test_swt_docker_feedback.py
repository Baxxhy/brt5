from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class SWTFeedbackTests(unittest.TestCase):
    def test_feedback_scripts_parse(self) -> None:
        for name in ("run_swt_pred_pre_feedback.py", "repair_swt_from_buggy_feedback.py"):
            ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8"))

    def test_buggy_feedback_does_not_apply_golden_patches(self) -> None:
        runner = (ROOT / "scripts" / "run_swt_pred_pre_feedback.py").read_text(encoding="utf-8")
        self.assertIn("spec.patch_list = [patch]", runner)
        self.assertNotIn("golden_code_patch]", runner)
        self.assertNotIn("golden_test_patch]", runner)
        self.assertIn("install_quiet_eval_diagnostics(skip_reinstall=True)", runner)

    def test_one_invalid_repair_does_not_abort_batch(self) -> None:
        repair = (ROOT / "scripts" / "repair_swt_from_buggy_feedback.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("future.result()", repair)
        self.assertIn("except Exception as exc", repair)
        self.assertIn("carry_source_after_repair_failure", repair)
        self.assertIn('"action": "repair_failed"', repair)
        self.assertIn('"carried_previous_candidate": True', repair)
        self.assertIn("completed_result(", repair)
        self.assertIn("args.output_generation, instance_id, args.round", repair)
        self.assertIn('summary.get("docker_feedback_round") == round_id', repair)
        self.assertIn('status="PROTECTION_VIOLATION"', repair)
        self.assertIn('"final_retry": True', repair)

    def test_official_log_classification_preserves_assertion_failures(self) -> None:
        module_path = ROOT / "scripts" / "repair_swt_from_buggy_feedback.py"
        spec = importlib.util.spec_from_file_location("repair_swt_feedback", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        assertion = module.execution_from_log(
            "sympy__sympy-1",
            "tests finished: 0 passed, 1 failed, in 0.01 seconds\nAssertionError",
        )
        passed = module.execution_from_log("pytest-dev__pytest-1", "1 passed in 0.02s")
        setup = module.execution_from_log(
            "django__django-1", "ImportError: Failed to import test module"
        )
        literal = module.execution_from_log(
            "pytest-dev__pytest-2",
            "collected 1 item\nE assert result == 'SyntaxError'\n1 failed\n",
        )
        self.assertEqual(assertion.status, "ASSERTION_FAIL")
        self.assertEqual(passed.status, "PASS")
        self.assertEqual(setup.status, "SETUP_ERROR")
        self.assertEqual(literal.status, "ASSERTION_FAIL")

    def test_skipped_test_is_not_hidden_by_cleanup_error(self) -> None:
        module_path = ROOT / "scripts" / "repair_swt_from_buggy_feedback.py"
        spec = importlib.util.spec_from_file_location("repair_swt_feedback_skip", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        result = module.execution_from_log(
            "django__django-1",
            "collected 1 item\n1 skipped in 0.01s\n"
            "git apply /root/pre_state.patch\nerror: unrecognized input\n",
        )
        self.assertEqual(result.status, "COLLECT_ERROR")

    def test_unittest_ok_is_not_hidden_by_cleanup_error(self) -> None:
        module_path = ROOT / "scripts" / "repair_swt_from_buggy_feedback.py"
        spec = importlib.util.spec_from_file_location("repair_swt_feedback_unittest", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        result = module.execution_from_log(
            "django__django-1",
            "test_target (queries.TestCase) ... ok\n"
            "Ran 1 test in 0.003s\n\nOK\n"
            "git apply /root/pre_state.patch\nerror: unrecognized input\n",
        )
        self.assertEqual(result.status, "PASS")

    def test_feature_skip_decorator_is_rejected_statically(self) -> None:
        from brt6.core.schema import BehaviorTarget
        from brt6.validation.semantic_guard import audit_candidate

        problem = audit_candidate(
            BehaviorTarget("x", expected_behavior={"text": "raise NotSupportedError"}),
            "@skipUnlessDBFeature('feature')\ndef test_target():\n    api()\n",
        )
        self.assertIn("不得使用会跳过完整测试", problem)

    def test_repeated_model_skip_decorator_is_removed(self) -> None:
        from brt6.generation.generator import _remove_test_skip_decorators

        code, removed = _remove_test_skip_decorators(
            "@skipUnlessDBFeature('feature')\n"
            "def test_target():\n    api()\n"
        )
        self.assertEqual(removed, ["skipunlessdbfeature"])
        self.assertNotIn("@skip", code)
        ast.parse(code)

    def test_title_raise_contract_requires_exception_oracle(self) -> None:
        from brt6.core.schema import BehaviorTarget
        from brt6.validation.semantic_guard import audit_candidate

        behavior = BehaviorTarget(
            "x", expected_behavior={"text": "The operation must raise/reject."}
        )
        self.assertIn(
            "没有异常 oracle",
            audit_candidate(behavior, "def test_target():\n    assert api() == 1\n"),
        )
        self.assertEqual(
            audit_candidate(
                behavior,
                "def test_target():\n    with pytest.raises(NotSupportedError):\n        api()\n",
            ),
            "",
        )

    def test_missing_expected_exception_is_deterministically_accepted(self) -> None:
        module_path = ROOT / "scripts" / "repair_swt_from_buggy_feedback.py"
        spec = importlib.util.spec_from_file_location("repair_swt_feedback_contract", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        from brt6.core.schema import BehaviorTarget, CandidateTest, ExecutionResult

        assert module.explicit_missing_raise_contract(
            BehaviorTarget("x", expected_behavior={"text": "operation must raise/reject"}),
            CandidateTest("x", code="with self.assertRaises(NotSupportedError):\n    api()\n"),
            ExecutionResult("x", status="ASSERTION_FAIL", stdout="NotSupportedError not raised"),
        )

    def test_semantic_verifier_failure_preserves_buggy_assertion(self) -> None:
        repair = (ROOT / "scripts" / "repair_swt_from_buggy_feedback.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("semantic verifier unavailable; preserve executed buggy assertion", repair)
        self.assertIn('decision.reason == "断言失败但是否对齐不确定。"', repair)

    def test_launcher_keeps_final_official_evaluation(self) -> None:
        launcher = (ROOT / "scripts" / "run_swt_iterative_docker_full.sh").read_text(encoding="utf-8")
        self.assertIn("run_swt_official_clean_full.sh", launcher)
        self.assertIn("FEEDBACK_ROUNDS", launcher)
        self.assertIn("predictions_repaired_only.jsonl", launcher)
        self.assertIn('{"repaired", "repair_failed", "fallback"}', launcher)
        self.assertIn("/dev/tcp/172.18.0.1/17890", launcher)
        self.assertIn('TMPDIR=${TMPDIR:-$RUN_DIR/tmp}', launcher)
        self.assertIn('repair_args=(--allow-missing)', launcher)
        self.assertIn('touch "$round_root/execution.done"', launcher)
        self.assertIn('touch "$round_root/repair.done"', launcher)
        self.assertIn("ACTIVE_INSTANCES=$RUN_DIR/tmp/active_instances.json", launcher)
        self.assertIn('-e EXECUTION_TIMEOUT -e MODEL -e INSTANCE_IDS_FILE', launcher)
        self.assertIn('--forced_seed_index "$seed_index"', launcher)
        self.assertIn("adaptive_recovery", launcher)
        self.assertIn("run_portfolio_buggy primary", launcher)
        self.assertIn("run_portfolio_surrogate direct", launcher)
        self.assertIn("generate_swt_surrogate_predictions.py", launcher)
        self.assertIn("summarize_swt_buggy_execution.py", launcher)
        self.assertIn("--surrogate-summary", launcher)
        self.assertIn("prepare_failure_directed_run.py", launcher)
        self.assertIn("summarize_failure_directed_run.py", launcher)

    def test_failure_directed_provenance_is_complete(self) -> None:
        repair = (ROOT / "scripts" / "repair_swt_from_buggy_feedback.py").read_text(
            encoding="utf-8"
        )
        for field in (
            "parent_candidate", "failure_signal", "mutation_family",
            "editable_regions", "protected_regions", "target_path_before",
            "target_path_after", "trigger_changed", "oracle_changed",
            "accepted", "fallback", "fallback_reason",
        ):
            self.assertIn(f'"{field}"', repair)
        self.assertIn("enforce_protection", repair)
        self.assertIn("protection_retry_count", repair)
        self.assertIn("golden_or_fixed_used", repair)

    def test_full_launcher_does_not_nest_controller(self) -> None:
        launcher = (ROOT / "scripts" / "run_swt_docker_full.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "BRT6_IN_ITERATIVE_CONTROLLER=1 BRT6_IN_OFFICIAL_CONTROLLER=1",
            launcher,
        )
        self.assertIn(
            'SWT_ENV_COMPAT=1 bash "$PROJECT_ROOT/scripts/prepare_matplotlib_compat_envs.sh"',
            launcher,
        )
        self.assertIn('DIRECT_GENERATION=$RUN_DIR/generation_direct', launcher)
        self.assertIn('--mutation false', launcher)

    def test_formal_eval_preserves_instance_cache(self) -> None:
        runner = (ROOT / "scripts" / "run_swt_official_clean_full.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("--cache_level instance --clean false", runner)
        self.assertIn('mkdir -p "$RUN_DIR/official_workspace"', runner)
        self.assertIn('evaluation/metrics.json', runner)
        summarizer = (ROOT / "scripts" / "summarize_swt_f2p.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('get("coverage_pred")', summarizer)
        self.assertIn('get("coverage_delta_pred")', summarizer)


if __name__ == "__main__":
    unittest.main()
