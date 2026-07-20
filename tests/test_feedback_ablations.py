from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from brt5.core.ablation import AblationConfig, behavior_prompt_payload
from brt5.core.prompts import (
    JOINT_SEED_GENERATION_SYSTEM_PROMPT,
    JOINT_SEED_GENERATION_USER_PROMPT,
    JOINT_SEED_OBSERVATION_ORACLE_SYSTEM_PROMPT,
    JOINT_SEED_PROTOCOL_RECOVERY_SYSTEM_PROMPT,
    JOINT_SEED_STRICT_VERIFIER_SYSTEM_PROMPT,
    JOINT_SEED_VERIFIER_SYSTEM_PROMPT,
    JOINT_SEED_VERIFIER_USER_PROMPT,
)
from brt5.core.schema import (
    BehaviorTarget,
    CandidateTest,
    ExecutionResult,
    FinalResult,
    HostContext,
    InstanceContext,
    MutationPlan,
    MutationStep,
    ProtocolRecovery,
    RetrievedTest,
    StrictVerifierResult,
    VerifierDecision,
)
from brt5.execution.feedback import (
    _joint_seed_references,
    _repair_focus,
    _uses_adaptive_seed_pipelines,
    run_instance_pipeline,
)
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
            reference_seed_tests=[
                {
                    "rank": rank,
                    "file": f"tests/test_seed_{rank}.py",
                    "name": f"test_seed_{rank}",
                    "code_content": f"def test_seed_{rank}():\n    assert {rank} >= 0\n",
                }
                for rank in range(3)
            ],
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
            for forbidden in ("实验条件", "消融", "关闭", "不使用显式", "不提供显式"):
                self.assertNotIn(forbidden, prompt)
            self.assertIn("Top-3 联合参考流程", prompt)
            positions = [prompt.index(f"test_seed_{rank}") for rank in range(3)]
            self.assertEqual(positions, sorted(positions))
            mutation_files = [
                path for path in Path(tmp).rglob("*")
                if path.is_file() and "mutation" in path.name.lower()
            ]
            self.assertEqual(mutation_files, [])

    def test_joint_seed_bundle_preserves_icore_top3_order(self) -> None:
        tests = [
            RetrievedTest(
                "demo__repo-1",
                name=f"test_rank_{rank}",
                file=f"tests/test_{rank}.py",
                code_content=f"def test_rank_{rank}(): pass",
            )
            for rank in range(4)
        ]
        bundle = _joint_seed_references(tests)
        self.assertEqual([item["rank"] for item in bundle], [0, 1, 2])
        self.assertEqual(
            [item["name"] for item in bundle],
            ["test_rank_0", "test_rank_1", "test_rank_2"],
        )

    def test_joint_prompt_family_describes_only_joint_workflow(self) -> None:
        prompts = (
            JOINT_SEED_GENERATION_SYSTEM_PROMPT,
            JOINT_SEED_GENERATION_USER_PROMPT,
            JOINT_SEED_PROTOCOL_RECOVERY_SYSTEM_PROMPT,
            JOINT_SEED_STRICT_VERIFIER_SYSTEM_PROMPT,
            JOINT_SEED_OBSERVATION_ORACLE_SYSTEM_PROMPT,
            JOINT_SEED_VERIFIER_SYSTEM_PROMPT,
            JOINT_SEED_VERIFIER_USER_PROMPT,
        )
        forbidden = (
            "mutation",
            "变异",
            "消融",
            "关闭",
            "不使用显式",
            "不提供显式",
            "另一个流程",
            "完整方法",
        )
        combined = "\n".join(prompts).lower()
        for token in forbidden:
            self.assertNotIn(token.lower(), combined)
        self.assertIn("top-3", combined)

    def test_seed_pipeline_routing_is_single_factor(self) -> None:
        common = {
            "adaptive_disabled": False,
            "generate_only": False,
            "protocol_recovery_enabled": True,
            "forced_seed_index": None,
        }
        self.assertTrue(_uses_adaptive_seed_pipelines(AblationConfig(), **common))
        self.assertFalse(
            _uses_adaptive_seed_pipelines(
                AblationConfig(mutation=False), **common
            )
        )

    def test_nonadherent_plan_candidate_uses_explicit_direct_fallback(self) -> None:
        behavior = BehaviorTarget(
            "demo__repo-1",
            expected_behavior={"text": "target_api returns the public result"},
        )
        host = HostContext(
            "demo__repo-1",
            host_file="tests/test_mod.py",
            seed_test_code=(
                "def test_seed():\n"
                "    result = target_api(1)\n"
                "    assert result == 1\n"
            ),
        )
        plan = MutationPlan(
            "demo__repo-1",
            status="VALID",
            steps=[
                MutationStep(
                    op="ARG_VALUE_REPLACE",
                    target_file="pkg/mod.py",
                    target_symbol="target_api",
                    seed_anchor="target_api(1)",
                    before="target_api(1)",
                    after="target_api(2)",
                    risk="low",
                )
            ],
            risk="low",
        )
        llm = Mock()
        llm.chat.side_effect = [
            "def test_generated():\n    assert target_api(3) == 3\n",
            "def test_generated():\n    assert target_api(4) == 4\n",
        ]
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
                mutation_plan=plan,
                ablation_config=AblationConfig(),
            )
            fallback = Path(tmp) / "mutation_round_0_fallback.json"
            self.assertTrue(fallback.is_file())

        self.assertEqual(candidate.mutation_plan_status, "FALLBACK_DIRECT")
        self.assertEqual(candidate.mutation_adherence["status"], "NOT_APPLICABLE")
        self.assertIn("target_api(4)", candidate.code)
        self.assertEqual(llm.chat.call_count, 2)

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
        retrieved_tests = [seed] + [
            RetrievedTest(
                instance_id,
                name=f"test_reference_{rank}",
                file=f"tests/test_reference_{rank}.py",
                code_content=f"def test_reference_{rank}():\n    assert True\n",
            )
            for rank in (1, 2)
        ]
        context = InstanceContext(
            instance_id,
            "full issue text",
            repo="demo/repo",
            buggy_repo_path=str(repo),
            retrieved_tests=retrieved_tests,
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
            return_value=MutationPlan(
                instance_id,
                status="VALID",
                steps=[MutationStep(op="ARG_VALUE_REPLACE")],
            )
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
        self.assertEqual(result.seed_mode, "joint_top3")
        self.assertEqual(result.seed_attempts_count, 3)
        self.assertEqual(len(result.host_context["reference_seed_tests"]), 3)
        self.assertEqual(list(output.rglob("mutation*")), [])

    def test_full_method_limits_trigger_replanning_to_one_call(self) -> None:
        result, planner, repair, _, _, _, temp = self._run_forced_decisions(
            AblationConfig(),
            ["repair_trigger", "repair_trigger", "repair_trigger", "reject"],
        )
        self.addCleanup(temp.cleanup)
        self.assertEqual(planner.call_count, 2)  # initial + one feedback replan
        self.assertEqual(result.trigger_replan_calls, 1)
        self.assertGreaterEqual(repair.call_count, 2)

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

    def test_assertion_feedback_on_uses_general_oracle_repair_without_probe(self) -> None:
        result, _, repair, _, rebind, _, temp = self._run_forced_decisions(
            AblationConfig(), ["repair_oracle", "accept"]
        )
        self.addCleanup(temp.cleanup)
        self.assertEqual(repair.call_args_list[0].args[8], "oracle")
        rebind.assert_not_called()
        self.assertEqual(result.repair_route_counts["assertion"], 1)

    def test_reject_oracle_failure_class_routes_to_oracle(self) -> None:
        focus = _repair_focus(
            VerifierDecision("demo", "reject", "oracle is too strong"),
            StrictVerifierResult(
                "demo",
                decision="reject",
                failure_class="oracle_too_strong",
            ),
            ExecutionResult("demo", returncode=1, status="ASSERTION_FAIL"),
        )
        self.assertEqual(focus, "oracle")

    def test_side_path_logging_observation_routes_to_oracle(self) -> None:
        focus = _repair_focus(
            VerifierDecision(
                "demo",
                "reject",
                "assertLogs uses the wrong logger name",
            ),
            StrictVerifierResult(
                "demo",
                decision="reject",
                failure_class="side_path",
                reason="logging observation selected an unsupported logger",
            ),
            ExecutionResult("demo", returncode=1, status="ASSERTION_FAIL"),
        )
        self.assertEqual(focus, "oracle")

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
