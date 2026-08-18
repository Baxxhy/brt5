#!/usr/bin/env python3
"""Apply the brt5 verifier/repair loop to official buggy-side Docker logs."""

from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import fields
import json
import os
from pathlib import Path
import re
import shutil
import textwrap
import traceback

from brt6.core.ablation import AblationConfig
from brt6.core.schema import (
    BehaviorTarget,
    CandidateTest,
    ExecutionResult,
    HostContext,
    ProtocolRecovery,
    RetrievedCode,
    VerifierDecision,
)
from brt6.execution.executor import classify_execution
from brt6.generation.generator import repair_candidate
from brt6.llm.llm_client import LLMClient
from brt6.issue.issue_rewriter import (
    apply_behavior_safety_constraints,
    apply_issue_authority_constraints,
    behavior_from_dict,
)
from brt6.mutation.failure_directed import (
    HARD_FAILURES,
    MutationFamily,
    candidate_id,
    contract_prompt,
    enforce_protection,
    infer_target_path,
    is_execution_regression,
    route_mutation,
)
from brt6.validation.verifier import verify_buggy_only


def construct(cls, payload: dict):
    allowed = {item.name for item in fields(cls)}
    return cls(**{key: value for key, value in payload.items() if key in allowed})


def load_code(path: Path) -> dict[str, list[RetrievedCode]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, list[RetrievedCode]] = {}
    for instance_id, objects in raw.items():
        values = objects.values() if isinstance(objects, dict) else objects
        result[instance_id] = [
            RetrievedCode(instance_id=instance_id, raw=item, **{
                key: value for key, value in item.items()
                if key in {field.name for field in fields(RetrievedCode)} and key not in {"instance_id", "raw"}
            })
            for item in values if isinstance(item, dict)
        ]
    return result


def execution_from_log(instance_id: str, text: str) -> ExecutionResult:
    """Recover the generated-test outcome from the official Docker transcript.

    The transcript also contains setup and cleanup shell tracing, so a bare
    return-code guess or the presence of the word ``Traceback`` is not enough.
    Prefer explicit test-run summaries and keep assertion failures distinct
    from collection/setup failures.
    """
    low = text.lower()
    timed_out = any(marker in low for marker in ("timed out", "timeout after", "exit code: 124"))
    passed_count = sum(int(value) for value in re.findall(r"\b(\d+)\s+passed\b", low))
    failed_count = sum(int(value) for value in re.findall(r"\b(\d+)\s+failed\b", low))
    skipped_count = sum(int(value) for value in re.findall(r"\b(\d+)\s+skipped\b", low))
    sympy_summary = re.findall(
        r"tests finished:\s*(\d+)\s+passed(?:,\s*(\d+)\s+failed)?", low
    )
    sympy_passed = sum(int(passed) for passed, _ in sympy_summary)
    sympy_failed = sum(int(failed or 0) for _, failed in sympy_summary)
    unittest_runs = [int(value) for value in re.findall(r"\bran\s+(\d+)\s+tests?\b", low)]
    unittest_count = max(unittest_runs, default=0)
    unittest_ok = bool(unittest_count and re.search(r"(?m)^ok$", low))
    assertion_failure = bool(
        "assertionerror" in low
        or sympy_failed
        or failed_count
        or re.search(r"(?m)^failed\s+\S+::\S+", low)
        or re.search(r"(?m)^test_\S+\s+\.\.\.\s+fail$", low)
        or "failed (failures=" in low
    )
    setup_failure = any(marker in low for marker in (
        "modulenotfounderror", "importerror", "error collecting",
        "failed to import test module", "minversion' requires",
        "fixture '" , "settings are not configured", "appregistrynotready",
    )) or "failed (errors=" in low
    zero_test = any(marker in low for marker in (
        "collected 0 items", "no tests ran", "no tests collected", "ran 0 tests",
    ))
    syntax_failure = "syntaxerror" in low or "indentationerror" in low
    explicit_pass = bool(
        passed_count
        or sympy_passed
        or unittest_ok
        or re.search(r"(?m)^ok(?:\s|$)", text)
        or re.search(r"(?m)^test_\S+\s+\.\.\.\s+ok$", low)
    )

    # Error names can legitimately occur inside a collected test's source or
    # assertion output.  Setup/syntax/collection labels are valid only when no
    # test actually ran; otherwise preserve the real buggy failure.
    executed_count = passed_count + failed_count + sympy_passed + sympy_failed + unittest_count
    reached_test = bool(executed_count or skipped_count)

    if timed_out:
        status = "TIMEOUT"
    elif assertion_failure:
        status = "ASSERTION_FAIL"
    elif not reached_test and syntax_failure:
        status = "SYNTAX_ERROR"
    elif not reached_test and setup_failure:
        status = "SETUP_ERROR"
    elif skipped_count and not (passed_count or failed_count or sympy_passed or sympy_failed):
        status = "COLLECT_ERROR"
    elif not reached_test and zero_test:
        status = "COLLECT_ERROR"
    elif explicit_pass:
        status = "PASS"
    else:
        status = classify_execution(1, text, "", False)
    returncode = 0 if status == "PASS" else 124 if status == "TIMEOUT" else 1
    return ExecutionResult(
        instance_id=instance_id,
        command="official SWTBench Docker buggy-side execution",
        returncode=returncode,
        stdout=text,
        status=status,
    )


def explicit_missing_raise_contract(
    behavior: BehaviorTarget, candidate: CandidateTest, execution: ExecutionResult
) -> bool:
    """Recognize the exact buggy failure required by an Issue-title contract."""
    expected = str(behavior.expected_behavior.get("text") or "").lower()
    requires_raise = any(
        marker in expected
        for marker in ("must raise", "should raise", "raise/reject", "必须抛", "应抛")
    )
    has_raise_oracle = bool(
        re.search(r"(?:pytest\.raises|assertRaises|assert_raises|\braises\s*\()", candidate.code)
    )
    log = (execution.stdout + "\n" + execution.stderr).lower()
    missing_raise = bool(re.search(r"(?:exception|error)?\s*not raised|did not raise", log))
    return bool(
        requires_raise
        and has_raise_oracle
        and execution.status in {"ASSERTION_FAIL", "ISSUE_ALIGNED_FAIL"}
        and missing_raise
    )


def carry_source_after_repair_failure(
    instance_id: str,
    source_dir: Path,
    output_dir: Path,
    error: BaseException,
    round_id: int = -1,
) -> dict:
    """Keep the previous candidate and record one isolated repair failure.

    A malformed LLM response is a candidate-level failure, not a batch-level
    infrastructure failure.  The unchanged previous candidate must continue
    through later export/formal evaluation, where the official grader decides
    whether it passes.  Copying (instead of symlinking) lets us attach the
    diagnostic without modifying the previous generation.
    """
    source = source_dir / instance_id
    target = output_dir / instance_id
    if target.exists() or target.is_symlink():
        target.unlink() if target.is_symlink() else shutil.rmtree(target)
    shutil.copytree(source, target, symlinks=True)
    failure = {
        "instance_id": instance_id,
        "action": "repair_failed",
        "error_type": type(error).__name__,
        "error": str(error),
        "carried_previous_candidate": True,
        "counts_as_success": False,
    }
    (target / "docker_feedback_repair_failure.json").write_text(
        json.dumps(
            {**failure, "traceback": "".join(traceback.format_exception(error))},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    code = (source / "final_test.py").read_text(encoding="utf-8", errors="replace")
    provenance = {
        "schema_version": "failure-directed-mutation.v1",
        "instance_id": instance_id,
        "candidate_id": candidate_id(instance_id, code),
        "parent_candidate": candidate_id(instance_id, code),
        "round": round_id,
        "seed_index": None,
        "failure_signal": f"repair exception: {type(error).__name__}",
        "mutation_family": "UNKNOWN",
        "editable_regions": [],
        "protected_regions": [],
        "target_path_before": None,
        "target_path_after": None,
        "trigger_changed": False,
        "oracle_changed": False,
        "accepted": False,
        "fallback": True,
        "fallback_reason": "repair_exception_parent_carried",
        "golden_or_fixed_used": False,
    }
    (target / "failure_directed_mutation.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return failure


def load_parent_provenance(source: Path) -> dict:
    path = source / "failure_directed_mutation.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def deterministic_structural_repair(code: str) -> str:
    """Apply only semantics-neutral formatting repairs before using the LLM."""
    stripped = code.strip()
    if stripped.startswith("```python") and stripped.endswith("```"):
        stripped = stripped[len("```python"): -3].strip()
    elif stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped[3:-3].strip()
    candidate = textwrap.dedent(stripped).rstrip() + "\n"
    try:
        ast.parse(candidate)
    except SyntaxError:
        return code
    return candidate


def write_mutation_provenance(target: Path, payload: dict) -> None:
    (target / "failure_directed_mutation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def completed_result(output_dir: Path, instance_id: str, round_id: int) -> dict | None:
    """Return a prior per-instance result when resuming an interrupted batch."""
    target = output_dir / instance_id
    if target.is_symlink():
        return {"instance_id": instance_id, "action": "accepted", "resumed": True}
    failure_path = target / "docker_feedback_repair_failure.json"
    if failure_path.is_file():
        result = json.loads(failure_path.read_text(encoding="utf-8"))
        result["resumed"] = True
        return result
    summary_path = target / "summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("docker_feedback_round") == round_id:
            return {
                "instance_id": instance_id,
                "action": "repaired",
                "focus": summary.get("docker_feedback_focus", "generic"),
                "resumed": True,
            }
    return None


def process_one(
    instance_id: str,
    issue: dict,
    source_dir: Path,
    output_dir: Path,
    log_path: Path,
    related_code: list[RetrievedCode],
    args: argparse.Namespace,
) -> dict:
    source = source_dir / instance_id
    target = output_dir / instance_id
    if not args.verify_only and (target.exists() or target.is_symlink()):
        target.unlink() if target.is_symlink() else shutil.rmtree(target)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    summary = json.loads((source / "summary.json").read_text(encoding="utf-8"))
    issue_text = str(issue.get("problem_statement") or "")
    behavior = behavior_from_dict(
        instance_id,
        json.loads((source / "behavior_target.json").read_text(encoding="utf-8")),
    )
    behavior = apply_issue_authority_constraints(issue_text, behavior)
    behavior = apply_behavior_safety_constraints(issue_text, behavior)
    host = construct(
        HostContext,
        json.loads((source / "host_context.json").read_text(encoding="utf-8")),
    )
    protocol_path = source / "protocol_recovery.json"
    protocol = construct(ProtocolRecovery, json.loads(protocol_path.read_text(encoding="utf-8"))) if protocol_path.is_file() else None
    code = (source / "final_test.py").read_text(encoding="utf-8")
    candidate = CandidateTest(
        instance_id=instance_id,
        round_id=args.round,
        code=code,
        candidate_file_path=str(target / f"candidate_round_{args.round}.py"),
        candidate_repo_path=str(summary.get("candidate_repo_path") or summary.get("direct_test_repo_path_hint") or ""),
        pytest_nodeid=str(summary.get("pytest_nodeid") or ""),
        command=str(summary.get("command") or ""),
    )
    execution = execution_from_log(instance_id, text)
    client = LLMClient(
        provider="deepseek",
        model=args.model,
        temperature=0.1,
        max_tokens=int(os.environ.get("BRT_FEEDBACK_MAX_TOKENS", "4096")),
    )
    decision = verify_buggy_only(
        issue_text,
        behavior,
        candidate,
        execution,
        client,
        host.to_dict(),
        "\n\n".join(item.code_content for item in related_code),
        AblationConfig(),
    )
    if explicit_missing_raise_contract(behavior, candidate, execution):
        decision = VerifierDecision(
            instance_id,
            "accept",
            "buggy execution did not raise the specific exception required by the Issue-title contract",
            ["trigger", "oracle"],
            "preserve candidate for issue-only surrogate and formal scoring",
        )
    # BRT5's semantic verifier is useful for repairing a wrong oracle, but an
    # unavailable verifier must not turn a genuine buggy assertion into an
    # automatic rewrite.  Preserve it and let official fixed-side scoring make
    # the final decision.
    if (
        execution.status in {"ASSERTION_FAIL", "ISSUE_ALIGNED_FAIL"}
        and decision.decision == "repair_oracle"
        and decision.reason == "断言失败但是否对齐不确定。"
    ):
        decision = VerifierDecision(
            instance_id,
            "accept",
            "semantic verifier unavailable; preserve executed buggy assertion",
            ["trigger", "oracle"],
            "preserve candidate for official fixed-side scoring",
        )
    parent_provenance = load_parent_provenance(source)
    route = route_mutation(
        behavior,
        candidate.code,
        execution.status,
        text,
        decision,
        parent_provenance,
    )
    if parent_provenance:
        parent_provenance["target_path_after"] = route.target_path_reached
        parent_provenance["execution_status_after"] = execution.status
        parent_provenance["outcome_evidence"] = route.evidence
        write_mutation_provenance(source, parent_provenance)
    regression = is_execution_regression(
        parent_provenance, execution.status, route.target_path_reached
    )
    if args.verify_only:
        return {
            "instance_id": instance_id,
            "action": "accepted" if decision.decision == "accept" else "rejected",
            "status": execution.status,
            "returncode": execution.returncode,
            "decision": decision.to_dict(),
            "verified_candidate": True,
            "failure_directed_route": route.to_dict(),
            "execution_regression": regression,
        }

    shutil.copytree(source, target, symlinks=True)
    (target / "prompts").mkdir(exist_ok=True)
    (target / "responses").mkdir(exist_ok=True)
    (target / "parent_candidate.py").write_text(candidate.code, encoding="utf-8")
    (target / "parent_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    parent_id = candidate_id(instance_id, candidate.code)
    base_provenance = {
        "schema_version": "failure-directed-mutation.v1",
        "instance_id": instance_id,
        "parent_candidate": parent_id,
        "round": args.round,
        "seed_index": summary.get("forced_seed_index"),
        **route.to_dict(),
        "target_path_before": route.target_path_reached,
        "target_path_after": None,
        "execution_status_before": execution.status,
        "golden_or_fixed_used": False,
    }
    if regression and (source / "parent_candidate.py").is_file():
        restored = (source / "parent_candidate.py").read_text(encoding="utf-8")
        restored_summary = (
            json.loads((source / "parent_summary.json").read_text(encoding="utf-8"))
            if (source / "parent_summary.json").is_file() else summary
        )
        (target / "final_test.py").write_text(restored, encoding="utf-8")
        (target / f"candidate_round_{args.round}.py").write_text(restored, encoding="utf-8")
        restored_summary.update({
            "docker_feedback_round": args.round,
            "docker_feedback_focus": "fallback",
            "docker_feedback_uses_golden_patch": False,
        })
        (target / "summary.json").write_text(
            json.dumps(restored_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        provenance = {
            **base_provenance,
            "candidate_id": candidate_id(instance_id, restored),
            "trigger_changed": False,
            "oracle_changed": False,
            "accepted": False,
            "fallback": True,
            "fallback_reason": regression,
        }
        write_mutation_provenance(target, provenance)
        return {
            "instance_id": instance_id,
            "action": "fallback",
            "focus": "fallback",
            "status": execution.status,
            "decision": decision.to_dict(),
            "failure_directed_route": route.to_dict(),
            "fallback_reason": regression,
        }

    family_focus = {
        MutationFamily.STRUCTURAL_REPAIR: "setup",
        MutationFamily.ORACLE_MUTATION: "oracle",
        MutationFamily.STATE_MUTATION: "trigger",
        MutationFamily.CALL_SEQUENCE_MUTATION: "trigger",
        MutationFamily.INPUT_MUTATION: "trigger",
    }
    focus = family_focus[route.family]
    contract = contract_prompt(route)
    repaired_code = candidate.code
    rule_repaired = False
    if route.family == MutationFamily.STRUCTURAL_REPAIR:
        repaired_code = deterministic_structural_repair(candidate.code)
        rule_repaired = repaired_code != candidate.code
    if not rule_repaired:
        repaired = repair_candidate(
            instance_id,
            behavior,
            host,
            candidate,
            execution,
            client,
            str(target),
            args.round,
            focus,
            related_code,
            verifier_feedback={**decision.to_dict(), "failure_directed_route": route.to_dict()},
            protocol=protocol,
            ablation_config=AblationConfig(),
            issue_text=issue_text,
            mutation_contract=contract,
        )
        repaired_code = repaired.code
    protection = enforce_protection(route.family, candidate.code, repaired_code, behavior)
    protection_retries = 0
    if not protection.accepted:
        protection_retries = 1
        (target / f"candidate_round_{args.round}_protection_violation.py").write_text(
            repaired_code, encoding="utf-8"
        )
        retry_execution = ExecutionResult(
            instance_id=instance_id,
            command="static protected-region enforcement",
            returncode=1,
            stdout="\n".join(protection.violations),
            status="PROTECTION_VIOLATION",
        )
        retry_contract = {
            **contract,
            "previous_violations": protection.violations,
            "retry_number": 1,
            "final_retry": True,
        }
        retry = repair_candidate(
            instance_id,
            behavior,
            host,
            candidate,
            retry_execution,
            client,
            str(target),
            args.round * 100 + 1,
            focus,
            related_code,
            verifier_feedback={**decision.to_dict(), "protection_violations": protection.violations},
            protocol=protocol,
            ablation_config=AblationConfig(),
            issue_text=issue_text,
            mutation_contract=retry_contract,
        )
        repaired_code = retry.code
        protection = enforce_protection(route.family, candidate.code, repaired_code, behavior)

    fallback = not protection.accepted
    fallback_reason = "protected_region_violation_after_retry" if fallback else ""
    final_code = candidate.code if fallback else repaired_code
    ast.parse(final_code)
    (target / "final_test.py").write_text(final_code, encoding="utf-8")
    (target / f"candidate_round_{args.round}.py").write_text(final_code, encoding="utf-8")
    provenance = {
        **base_provenance,
        "candidate_id": candidate_id(instance_id, final_code),
        "trigger_changed": protection.trigger_changed if not fallback else False,
        "oracle_changed": protection.oracle_changed if not fallback else False,
        "accepted": not fallback,
        "fallback": fallback,
        "fallback_reason": fallback_reason,
        "protected_region_violations": protection.violations,
        "protection_retry_count": protection_retries,
        "rule_repaired": rule_repaired,
    }
    write_mutation_provenance(target, provenance)
    summary.update({
        "rounds_used": args.round + 1,
        "candidate_repo_path": candidate.candidate_repo_path,
        "pytest_nodeid": candidate.pytest_nodeid,
        "command": candidate.command,
        "docker_feedback_round": args.round,
        "docker_feedback_focus": focus,
        "docker_feedback_decision": decision.to_dict(),
        "docker_feedback_uses_golden_patch": False,
        "failure_directed_mutation": provenance,
    })
    (target / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "instance_id": instance_id,
        "action": "fallback" if fallback else "repaired",
        "focus": focus,
        "status": execution.status,
        "decision": decision.to_dict(),
        "failure_directed_route": route.to_dict(),
        "fallback_reason": fallback_reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances", type=Path, required=True)
    parser.add_argument("--code-retrieval", type=Path, required=True)
    parser.add_argument("--source-generation", type=Path, required=True)
    parser.add_argument("--output-generation", type=Path, required=True)
    parser.add_argument("--feedback-root", type=Path, required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--model", default="DeepSeek-V4-Flash")
    parser.add_argument("--max-workers", type=int, default=10)
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Run the buggy-side verifier and record decisions without generating repairs.",
    )
    args = parser.parse_args()
    issues = {row["instance_id"]: row for row in json.loads(args.instances.read_text(encoding="utf-8"))}
    code = load_code(args.code_retrieval)
    args.output_generation.mkdir(parents=True, exist_ok=True)
    tasks = []
    results = []
    missing = []
    for instance_id in issues:
        log = args.feedback_root / instance_id / "test_output.txt"
        if not log.is_file():
            missing.append(instance_id)
            continue
        previous = None if args.verify_only else completed_result(
            args.output_generation, instance_id, args.round
        )
        if previous is not None:
            results.append(previous)
        else:
            tasks.append((instance_id, log))
    if missing and not args.allow_missing:
        raise RuntimeError(f"missing buggy-side Docker logs for {len(missing)} instances: {missing[:10]}")
    for instance_id in missing:
        if args.verify_only:
            results.append({
                "instance_id": instance_id,
                "action": "missing",
                "verified_candidate": False,
            })
            continue
        source = args.source_generation / instance_id
        target = args.output_generation / instance_id
        if target.exists() or target.is_symlink():
            target.unlink() if target.is_symlink() else shutil.rmtree(target)
        target.symlink_to(source, target_is_directory=True)
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {
            pool.submit(
                process_one, instance_id, issues[instance_id], args.source_generation,
                args.output_generation, log, code.get(instance_id, []), args,
            ): instance_id
            for instance_id, log in tasks
        }
        for future in as_completed(futures):
            instance_id = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # one model response must not abort 276
                if args.verify_only:
                    results.append({
                        "instance_id": instance_id,
                        "action": "verification_error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "verified_candidate": False,
                    })
                else:
                    results.append(carry_source_after_repair_failure(
                        instance_id,
                        args.source_generation,
                        args.output_generation,
                        exc,
                        args.round,
                    ))
    results.sort(key=lambda row: row["instance_id"])
    (args.output_generation / "docker_feedback_summary.json").write_text(
        json.dumps({
            "round": args.round,
            "results": results,
            "carried_without_rerun": missing,
            "golden_patches_used": False,
            "verify_only": args.verify_only,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
