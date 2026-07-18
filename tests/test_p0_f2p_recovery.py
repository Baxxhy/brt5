from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from brt5.context.host_context import rank_related_tests, select_related_test
from brt5.core.schema import (
    BehaviorTarget,
    CandidateTest,
    ExecutionResult,
    RetrievedTest,
    VerifierDecision,
)
from brt5.execution.executor import run_command_in_conda
from brt5.execution.feedback import _checkpoint_score, _save_checkpoint
from brt5.issue.issue_rewriter import (
    apply_behavior_safety_constraints,
    behavior_from_dict,
)
from brt5.validation.strict_semantic_verifier import verify_strict_semantics


class _StaticLLM:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls = 0

    def chat(self, system: str, prompt: str) -> str:
        self.calls += 1
        self.system = system
        self.prompt = prompt
        return json.dumps(self.response, ensure_ascii=False)


class P0SimpleLLMSelectorTests(unittest.TestCase):
    def test_behavior_target_three_part_round_trip_is_lossless(self) -> None:
        flat = {
            "issue_summary": "summary",
            "trigger_condition": {
                "text": "call bad input",
                "evidence": ["issue line"],
                "confidence": "high",
            },
            "error_symptom": {
                "text": "wrong message",
                "evidence": ["trace"],
                "confidence": "high",
            },
            "expected_behavior": {
                "text": "correct message",
                "evidence": ["issue expectation"],
                "confidence": "high",
            },
            "target_apis": [{"name": "DurationField.clean", "source_path": "fields.py"}],
            "suspected_bug_locations": [],
            "related_test_seeds": [{"test_file": "tests/model.py", "test_name": "test_invalid"}],
            "mutation_hints": [{"slot": "input", "confidence": "medium"}],
            "observation_points": [{"kind": "exception"}],
            "assertion_hints": [{"preferred_assertion_style": "contains_fragment"}],
            "setup_hints": [{"hint": "SimpleTestCase", "confidence": "high"}],
            "uncertainties": ["exact stable fragment"],
        }
        first = behavior_from_dict("django__django-11049", flat)
        persisted = first.to_dict()
        self.assertEqual(
            set(persisted)
            - {"schema_version", "instance_id", "issue_summary", "uncertainties", "raw"},
            {"setup", "trigger", "oracle"},
        )
        self.assertEqual(
            persisted["trigger"]["trigger_condition"]["evidence"], ["issue line"]
        )
        self.assertEqual(persisted["raw"], flat)
        second = behavior_from_dict("django__django-11049", persisted)
        self.assertEqual(second.trigger_condition, first.trigger_condition)
        self.assertEqual(second.expected_behavior, first.expected_behavior)
        self.assertEqual(second.related_test_seeds, first.related_test_seeds)
        self.assertEqual(second.uncertainties, first.uncertainties)

    def test_behavior_recommendation_cannot_reorder_icore(self) -> None:
        model = RetrievedTest(
            "x", "test_invalid_string", "tests/model_fields/test_durationfield.py", "model"
        )
        form = RetrievedTest(
            "x", "test_overflow", "tests/forms_tests/test_durationfield.py", "form"
        )
        behavior = BehaviorTarget(
            "x",
            related_test_seeds=[
                {"test_file": form.file, "test_name": form.name, "confidence": "high"}
            ],
        )
        self.assertIs(select_related_test([model, form], behavior), model)
        self.assertEqual(rank_related_tests([model, form], behavior), [model, form])

    def test_behavior_audit_preserves_issue_valid_example(self) -> None:
        behavior = BehaviorTarget(
            "django__django-11049",
            trigger_condition={"text": "Use 14:00 to trigger validation"},
            expected_behavior={"text": "The validation error message has the corrected format."},
            mutation_hints=[{"target_pattern": "Treat 14:00 as invalid"}],
        )
        apply_behavior_safety_constraints(
            "The value '14:00' translates to fourteen minutes and is accepted.",
            behavior,
        )
        self.assertEqual(behavior.safety_constraints[0]["protected_inputs"], ["14:00"])

    def test_executor_returns_real_buggy_log_without_dynamic_tracing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = run_command_in_conda(
                "python -c \"raise ValueError('buggy symptom')\"",
                raw,
                no_conda=True,
                instance_id="execution",
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("buggy symptom", result.stderr)
        self.assertNotIn("runtime_target_hit", result.to_dict())
        self.assertNotIn("target_hit_evidence", result.to_dict())

    def test_llm_accepts_issue_aligned_buggy_failure_without_dynamic_evidence(self) -> None:
        llm = _StaticLLM(
            {
                "decision": "accept",
                "failure_class": "issue_aligned",
                "target_hit": True,
                "oracle_grounded_in_issue": True,
                "uses_public_behavior": True,
                "reason": "The assertion captures the expected fixed behavior.",
                "next_action": "accept",
            }
        )
        candidate = CandidateTest("x", code="def test_x():\n    assert api() == 2\n")
        execution = ExecutionResult(
            "x",
            command="pytest test_x.py",
            returncode=1,
            stdout="FAILED expected 2, got 1",
            status="ASSERTION_FAIL",
        )
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir()
            Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                "api() currently returns 1 but should return 2.",
                BehaviorTarget("x", expected_behavior={"text": "api returns 2"}),
                None,
                candidate,
                execution,
                "def api(): return 1",
                llm,
                raw,
                0,
            )
            prompt = Path(raw, "prompts", "strict_verifier_round_0.txt").read_text()
        self.assertEqual(decision.decision, "accept")
        self.assertTrue(strict.target_hit)
        self.assertIn("currently returns 1 but should return 2", prompt)
        self.assertIn("FAILED expected 2, got 1", prompt)
        self.assertNotIn("动态目标命中", prompt)

    def test_simple_semantic_ranking_prefers_llm_accept(self) -> None:
        execution = ExecutionResult(returncode=1, status="ASSERTION_FAIL")
        accepted = VerifierDecision("x", "accept")
        rejected = VerifierDecision("x", "repair_oracle")
        strict = SimpleNamespace(
            failure_class="issue_aligned",
            target_hit=True,
            oracle_grounded_in_issue=True,
            uses_public_behavior=True,
        )
        accepted_score, _ = _checkpoint_score(execution, accepted, strict)
        rejected_score, _ = _checkpoint_score(execution, rejected, strict)
        self.assertGreater(accepted_score, rejected_score)

        with tempfile.TemporaryDirectory() as raw:
            candidate = CandidateTest("x", code="def test_x():\n    assert False\n")
            accepted_checkpoint = _save_checkpoint(
                raw, 0, candidate, execution, accepted, None, strict_result=strict
            )
            rejected_checkpoint = _save_checkpoint(
                raw, 1, candidate, execution, rejected, None, strict_result=strict
            )
        self.assertGreater(
            tuple(accepted_checkpoint.rank_key), tuple(rejected_checkpoint.rank_key)
        )
        self.assertEqual(accepted_checkpoint.oracle_risk, {})
        self.assertEqual(accepted_checkpoint.surrogate, {})

    def test_active_feedback_has_no_surrogate_call(self) -> None:
        import brt5.execution.feedback as feedback

        source = inspect.getsource(feedback)
        self.assertNotIn("run_surrogate_patch_loop(", source)
        self.assertNotIn("assess_surrogate_risk(", source)


if __name__ == "__main__":
    unittest.main()
