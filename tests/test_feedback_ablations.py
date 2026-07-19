from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from brt5.core.ablation import AblationConfig, behavior_prompt_payload
from brt5.core.schema import (
    BehaviorTarget,
    CandidateTest,
    ExecutionResult,
    FinalResult,
    HostContext,
    InstanceContext,
    MutationPlan,
    ProtocolRecovery,
    RetrievedTest,
    StrictVerifierResult,
    VerifierDecision,
)
from brt5.execution.feedback import run_instance_pipeline
from brt5.generation.generator import generate_candidate
from brt5.pipeline.run import (
    ablation_config_from_args,
    build_parser,
    resume_matches_ablation,
)


class _FakeLLM:
    def __init__(self, response: str = "def test_generated():\n    assert False\n") -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.response


class FeedbackAblationTests(unittest.TestCase):
    def _required_args(self) -> list[str]:
        return [
            "--instances_path", "issues.json",
            "--code_retrieval_path", "code.json",
            "--test_retrieval_path", "tests.json",
            "--repo_root_base", "repos",
            "--output_dir", "out",
        ]

    def test_config_defaults_and_every_single_ablation(self) -> None:
        parser = build_parser()
        full = ablation_config_from_args(parser.parse_args(self._required_args()))
        self.assertEqual(full.ablation_id, "full")
        self.assertTrue(full.compute_patch_coverage)
        cases = {
            "--behavior-target": "wo_behavior_target",
            "--mutation": "wo_mutation",
            "--specialized-feedback": "generic_iteration",
            "--environment-feedback": "wo_environment_feedback",
            "--trigger-feedback": "wo_trigger_feedback",
            "--assertion-feedback": "wo_assertion_feedback",
        }
        for flag, expected in cases.items():
            with self.subTest(flag=flag):
                config = ablation_config_from_args(
                    parser.parse_args(self._required_args() + [flag, "off"])
                )
                self.assertEqual(config.ablation_id, expected)
                self.assertFalse(config.compute_patch_coverage)
                self.assertEqual(len(config.disabled_components()), 1)

    def test_multiple_disabled_components_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            AblationConfig(mutation=False, trigger_feedback=False).validate()

    def test_resume_requires_exact_ablation_signature(self) -> None:
        full = AblationConfig()
        no_mutation = AblationConfig(mutation=False)
        summary = {"status": "ISSUE_ALIGNED_FAIL", "ablation_signature": full.signature}
        self.assertTrue(resume_matches_ablation(summary, full))
        self.assertFalse(resume_matches_ablation(summary, no_mutation))

    def test_mutation_payload_is_filtered_without_changing_cache(self) -> None:
        behavior = BehaviorTarget(
            "demo__repo-1",
            mutation_hints=[{"target_pattern": "bad value"}],
            raw={"mutation_hints": [{"raw": True}], "evidence": "kept"},
        )
        payload = behavior_prompt_payload(behavior, AblationConfig(mutation=False))
        serialized = json.dumps(payload)
        self.assertNotIn("mutation_hints", serialized)
        self.assertEqual(behavior.mutation_hints[0]["target_pattern"], "bad value")
        self.assertEqual(behavior.raw["mutation_hints"][0]["raw"], True)

    def test_no_mutation_prompt_and_artifacts_contain_no_mutation_language(self) -> None:
        behavior = BehaviorTarget(
            "demo__repo-1",
            issue_summary="demo",
            mutation_hints=[{"target_pattern": "hidden"}],
        )
        host = HostContext(
            "demo__repo-1",
            seed_test_code="def test_seed():\n    assert True\n",
        )
        llm = _FakeLLM()
        with tempfile.TemporaryDirectory() as tmp:
            candidate = generate_candidate(
                "demo__repo-1",
                behavior,
                host,
                None,
                [],
                llm,
                tmp,
                tmp,
                write_to_repo=False,
                ablation_config=AblationConfig(mutation=False),
            )
            self.assertTrue(candidate.code)
            prompt = (Path(tmp) / "prompts" / "generation_round_0.txt").read_text(
                encoding="utf-8"
            )
            lowered = prompt.lower()
            for forbidden in ("mutationplan", "mutation_plan", "mutation_hints", "mutation", "变异"):
                self.assertNotIn(forbidden, lowered)
            mutation_files = [
                path for path in Path(tmp).rglob("*")
                if path.is_file() and "mutation" in path.name.lower()
            ]
            self.assertEqual(mutation_files, [])

    def _run_forced_decisions(
        self,
        config: AblationConfig,
        decisions: list[str],
        executions: list[ExecutionResult] | None = None,
    ) -> tuple[FinalResult, Mock, Mock, Mock, Mock, Path, tempfile.TemporaryDirectory]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        repo = root / "repo"
        repo.mkdir()
        output = root / "output"
        output.mkdir()
        instance_id = "demo__repo-1"
        behavior = BehaviorTarget(instance_id, issue_summary="issue")
        seed = RetrievedTest(
            instance_id,
            name="test_seed",
            file="tests/test_seed.py",
            code_content="def test_seed():\n    assert True\n",
        )
        context = InstanceContext(
            instance_id,
            "full issue text",
            repo="demo/repo",
            buggy_repo_path=str(repo),
            retrieved_tests=[seed],
        )
        host = HostContext(
            instance_id,
            host_file=seed.file,
            seed_test_name=seed.name,
            seed_test_code=seed.code_content,
            seed_execution_status="PASS",
        )
        protocol = ProtocolRecovery(instance_id, test_file=seed.file)
        candidate_path = repo / "tests" / "test_brt.py"
        candidate_path.parent.mkdir()

        def make_candidate(*args, **kwargs):  # noqa: ANN002, ANN003
            if len(args) > 8 and isinstance(args[8], int):
                round_id = int(args[8])
            elif len(args) > 7 and isinstance(args[7], int):
                round_id = int(args[7])
            else:
                round_id = int(kwargs.get("round_id", 0))
            return CandidateTest(
                instance_id,
                round_id=round_id,
                code="def test_brt():\n    assert False\n",
                candidate_file_path=str(candidate_path),
                candidate_repo_path="tests/test_brt.py",
            )

        execution_values = executions or [
            ExecutionResult(
                instance_id,
                returncode=1,
                status="ASSERTION_FAIL",
                stdout="failed",
            )
            for _ in range(max(1, len(decisions)))
        ]
        strict_values = []
        for decision in decisions:
            strict = StrictVerifierResult(
                instance_id,
                decision=decision,
                failure_class="issue_aligned" if decision == "accept" else "side_path",
                target_hit=decision == "accept",
                oracle_grounded_in_issue=decision == "accept",
                uses_public_behavior=decision == "accept",
                reason=decision,
            )
            strict_values.append(
                (VerifierDecision(instance_id, decision, decision), strict)
            )

        build_plan = Mock(
            return_value=MutationPlan(instance_id, mutation_ops=["change_input"])
        )
        repair = Mock(side_effect=make_candidate)
        dependency = Mock(return_value=True)
        rebind = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch("brt5.execution.feedback._load_behavior_evidence", return_value=behavior))
            stack.enter_context(patch("brt5.execution.feedback.build_host_context", return_value=host))
            stack.enter_context(patch("brt5.execution.feedback.recover_test_protocol", return_value=protocol))
            stack.enter_context(patch("brt5.execution.feedback.audit_recovered_protocol", side_effect=lambda *args: args[0]))
            stack.enter_context(patch("brt5.execution.feedback.build_mutation_plan", build_plan))
            stack.enter_context(patch("brt5.execution.feedback.generate_candidate", side_effect=make_candidate))
            stack.enter_context(patch("brt5.execution.feedback.repair_candidate", repair))
            stack.enter_context(patch("brt5.execution.feedback._refresh_candidate_command"))
            stack.enter_context(patch("brt5.execution.feedback.run_command_in_conda", side_effect=execution_values))
            stack.enter_context(patch("brt5.execution.feedback.verify_strict_semantics", side_effect=strict_values))
            stack.enter_context(patch("brt5.execution.feedback._recover_declared_dependency", dependency))
            stack.enter_context(patch("brt5.execution.feedback.rebind_observation_oracle", rebind))
            result = run_instance_pipeline(
                context,
                object(),
                str(output),
                no_conda=True,
                max_feedback_rounds=3,
                max_env_rounds=2,
                max_brt_rounds=3,
                ablation_config=config,
                _adaptive_disabled=True,
                _prepared_repo_path=str(repo),
                _prepare_meta={"status": "PASS", "env_name": ""},
            )
        return result, build_plan, repair, dependency, rebind, output, temp

    def test_no_mutation_keeps_trigger_feedback_but_never_plans(self) -> None:
        result, planner, repair, _, _, output, temp = self._run_forced_decisions(
            AblationConfig(mutation=False), ["repair_trigger", "accept"]
        )
        self.addCleanup(temp.cleanup)
        planner.assert_not_called()
        self.assertEqual(repair.call_args_list[0].args[8], "trigger")
        self.assertEqual(result.mutation_plan_calls, 0)
        self.assertEqual(result.mutation_ops, [])
        self.assertEqual(result.repair_route_counts["trigger"], 1)
        self.assertEqual(list(output.rglob("mutation*")), [])

    def test_generic_iteration_uses_only_generic_repair(self) -> None:
        result, planner, repair, dependency, rebind, _, temp = self._run_forced_decisions(
            AblationConfig(specialized_feedback=False),
            ["repair_trigger", "accept"],
        )
        self.addCleanup(temp.cleanup)
        self.assertEqual(planner.call_count, 1)
        self.assertEqual([call.args[8] for call in repair.call_args_list], ["generic"])
        dependency.assert_not_called()
        rebind.assert_not_called()
        self.assertEqual(result.repair_route_counts["generic"], 1)
        for route in ("environment", "trigger", "assertion"):
            self.assertEqual(result.repair_route_counts[route], 0)

    def test_environment_feedback_off_stops_late_setup_repair(self) -> None:
        result, _, repair, dependency, _, _, temp = self._run_forced_decisions(
            AblationConfig(environment_feedback=False), ["repair_setup"]
        )
        self.addCleanup(temp.cleanup)
        repair.assert_not_called()
        dependency.assert_not_called()
        self.assertEqual(result.repair_route_counts["environment"], 0)

    def test_environment_feedback_off_records_initial_setup_error_without_recovery(self) -> None:
        setup_execution = ExecutionResult(
            "demo__repo-1",
            returncode=2,
            status="SETUP_ERROR",
            stderr="missing fixture",
        )
        result, _, repair, dependency, _, _, temp = self._run_forced_decisions(
            AblationConfig(environment_feedback=False),
            ["repair_setup"],
            executions=[setup_execution],
        )
        self.addCleanup(temp.cleanup)
        self.assertEqual(result.status, "ENV_UNRESOLVED")
        repair.assert_not_called()
        dependency.assert_not_called()
        self.assertEqual(result.repair_route_counts["environment"], 0)

    def test_trigger_feedback_off_stops_trigger_repair(self) -> None:
        result, planner, repair, _, _, _, temp = self._run_forced_decisions(
            AblationConfig(trigger_feedback=False), ["reject"]
        )
        self.addCleanup(temp.cleanup)
        self.assertEqual(planner.call_count, 1)
        repair.assert_not_called()
        self.assertEqual(result.repair_route_counts["trigger"], 0)

    def test_assertion_feedback_off_stops_observation_and_oracle_repair(self) -> None:
        result, _, repair, _, rebind, _, temp = self._run_forced_decisions(
            AblationConfig(assertion_feedback=False), ["repair_oracle"]
        )
        self.addCleanup(temp.cleanup)
        repair.assert_not_called()
        rebind.assert_not_called()
        self.assertEqual(result.repair_route_counts["assertion"], 0)

    def test_shell_rejects_multiple_off_before_runtime_checks(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "scripts" / "run_p0_simple_llm_selector_full.sh"
        proc = subprocess.run(
            ["bash", str(launcher), "--mutation", "off", "--trigger-feedback", "off"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("mutually exclusive", proc.stderr)


if __name__ == "__main__":
    unittest.main()
