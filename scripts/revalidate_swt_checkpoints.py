#!/usr/bin/env python3
"""Zero-LLM Docker revalidation and BRT5 checkpoint fallback selection."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
import re
import time

from brt6.core.ablation import AblationConfig
from brt6.core.schema import (
    BehaviorTarget,
    CandidateTest,
    ExecutionResult,
    HostContext,
    RetrievedCode,
)
from brt6.execution.feedback import _save_checkpoint
from brt6.validation.verifier import verify_buggy_only

from src.auxillary_src.extract_patches import remove_binary_diffs
from src.dataset import get_dataset_from_preds
from src.grading import get_logs_eval
from src.run_evaluation import run_eval_exec_spec
from src.test_spec import make_test_spec
from src.utils import get_test_directives

from export_swt_predictions import _new_file_patch, _safe_relative_path
from run_swt_official_env_compat import (
    apply_environment_compatibility,
    install_quiet_eval_diagnostics,
    install_retryable_repo_setup,
    install_retryable_source_fetches,
    install_tolerant_docker_output,
)


CATEGORY_TO_STATUS = {
    "pass": "PASS",
    "assertion_failure": "ASSERTION_FAIL",
    "import_error": "SETUP_ERROR",
    "collection_error": "COLLECT_ERROR",
    "setup_error": "SETUP_ERROR",
    "zero_test": "COLLECT_ERROR",
    "syntax_error": "SYNTAX_ERROR",
    "runtime_error": "UNRELATED_FAIL",
    "timeout": "TIMEOUT",
    "patch_apply_error": "SETUP_ERROR",
    "execution_missing": "SETUP_ERROR",
}


def construct(cls, payload: dict):
    """Construct schema objects without importing the model-backed repair CLI."""
    allowed = {item.name for item in fields(cls)}
    return cls(**{key: value for key, value in payload.items() if key in allowed})


def load_code(path: Path) -> dict[str, list[RetrievedCode]]:
    """Load retrieval context locally; this module has no LLM client dependency."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    allowed = {item.name for item in fields(RetrievedCode)} - {"instance_id", "raw"}
    result: dict[str, list[RetrievedCode]] = {}
    for instance_id, objects in raw.items():
        values = objects.values() if isinstance(objects, dict) else objects
        result[instance_id] = [
            RetrievedCode(
                instance_id=instance_id,
                raw=item,
                **{key: value for key, value in item.items() if key in allowed},
            )
            for item in values
            if isinstance(item, dict)
        ]
    return result


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def source_generations(source_run: Path, iterative_run: Path) -> list[Path]:
    return [source_run / "generation"] + [
        iterative_run / "feedback" / f"round_{round_id}" / "generation"
        for round_id in (1, 2, 3)
    ]


def build_manifest(instance_ids: list[str], generation_roots: list[Path]) -> dict:
    records = []
    unique = {}
    for instance_id in instance_ids:
        for round_id, root in enumerate(generation_roots):
            instance_dir = root / instance_id
            test_path = instance_dir / "final_test.py"
            summary_path = instance_dir / "summary.json"
            if not test_path.is_file():
                raise FileNotFoundError(f"missing Round{round_id} candidate: {test_path}")
            code = test_path.read_text(encoding="utf-8", errors="replace")
            digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
            key = f"{instance_id}:{digest}"
            record = {
                "instance_id": instance_id,
                "round_id": round_id,
                "sha256": digest,
                "unique_key": key,
                "test_path": str(test_path.resolve()),
                "summary_path": str(summary_path.resolve()),
            }
            records.append(record)
            unique.setdefault(key, record)
    return {
        "checkpoint_count": len(records),
        "unique_candidate_count": len(unique),
        "duplicate_count": len(records) - len(unique),
        "records": records,
        "unique_candidates": sorted(unique.values(), key=lambda row: (row["instance_id"], row["round_id"])),
    }


def result_path(output_dir: Path, record: dict) -> Path:
    return output_dir / "executions" / record["instance_id"] / f"{record['sha256']}.json"


def classify_test_output(path: Path, repo: str) -> dict:
    if not path.is_file():
        return {"category": "execution_missing", "patch_applied": False, "parsed_tests": {}, "evidence": "test_output.txt missing"}
    text = path.read_text(encoding="utf-8", errors="replace")
    lowered = text.lower()
    parsed, patch_applied = get_logs_eval(str(path), repo, "unit_test")
    statuses = Counter(parsed.values())
    collected = [int(value) for value in re.findall(r"collected\s+(\d+)\s+items?", lowered)]
    ran = [int(value) for value in re.findall(r"ran\s+(\d+)\s+tests?", lowered)]
    summary_counts = [int(value) for value in re.findall(r"\b(\d+)\s+(?:passed|failed|error|errors)\b", lowered)]
    executed_count = max([len(parsed), *ran, *summary_counts], default=0)
    collected_count = max(collected, default=0)

    import_failure = bool(re.search(
        r"(?mi)^(?:e\s+)?(?:importerror|modulenotfounderror):|"
        r"^importerror while importing test module",
        text,
    ))
    syntax_failure = bool(re.search(
        r"(?mi)^(?:e\s+)?(?:syntaxerror|indentationerror):|"
        r"^error collecting .*syntaxerror",
        text,
    ))
    assertion_failure = bool(re.search(
        r"(?mi)^(?:e\s+)?assertionerror(?::|$)|^e\s+assert\b",
        text,
    ))
    collection_failure = any(marker in lowered for marker in (
        "error collecting", "collection error", "not found:",
    ))
    setup_failure = bool(re.search(r"(?mi)^_+\s+error at setup", text)) or any(
        marker in lowered for marker in (
            "fixture '", "settings are not configured", "improperlyconfigured",
            "appregistrynotready", "requires pytest-", "unknown config option",
        )
    )
    observed = []
    if assertion_failure:
        observed.append("assertion_failure")
    if import_failure:
        observed.append("import_error")
    if collection_failure:
        observed.append("collection_error")
    if setup_failure:
        observed.append("setup_error")
    if syntax_failure:
        observed.append("syntax_error")

    if not patch_applied:
        category, evidence = "patch_apply_error", "official harness did not confirm applied patch"
    elif any(marker in lowered for marker in ("tests timed out", "timed out after", "timeout after")):
        category, evidence = "timeout", "official Docker execution timed out"
    elif any(marker in lowered for marker in ("collected 0 items", "no tests ran", "ran 0 tests", "no tests collected")) and executed_count == 0:
        category, evidence = "zero_test", "runner executed zero tests"
    # Setup/collection/import/syntax are primary only when the runner never
    # reached a real test.  Old projects often contain tests whose expected
    # output literally includes words such as SyntaxError or ImportError.
    elif executed_count == 0 and syntax_failure:
        category, evidence = "syntax_error", "Python syntax/indentation error prevented execution"
    elif executed_count == 0 and import_failure:
        category, evidence = "import_error", "import failed before test execution"
    elif executed_count == 0 and collection_failure:
        category, evidence = "collection_error", "runner could not collect the selected test"
    elif executed_count == 0 and setup_failure:
        category, evidence = "setup_error", "test harness/fixture/configuration failed"
    elif statuses.get("FAILED", 0) or statuses.get("ERROR", 0) or statuses.get("ERROR:", 0):
        if assertion_failure:
            category, evidence = "assertion_failure", "collected test reached an assertion failure"
        else:
            category, evidence = "runtime_error", "collected test failed with a non-assertion runtime error"
    elif "internalerror>" in lowered or "traceback (most recent call last)" in lowered:
        category, evidence = "runtime_error", "test execution reached an internal/runtime exception"
    elif statuses.get("PASSED", 0) or re.search(r"\b[1-9]\d*\s+passed\b", lowered):
        category, evidence = "pass", "at least one selected test executed and passed"
    elif executed_count > 0:
        category, evidence = "runtime_error", "tests executed but produced no official terminal status"
    else:
        category, evidence = "setup_error", "no trustworthy collected/executed test outcome"
    return {
        "category": category,
        "patch_applied": patch_applied,
        "parsed_tests": dict(sorted(parsed.items())),
        "status_counts": dict(sorted(statuses.items())),
        "collected_count": collected_count,
        "executed_count": executed_count,
        "observed_categories": observed,
        "evidence": evidence,
    }


def make_patch(record: dict) -> tuple[str, dict, str]:
    summary_path = Path(record["summary_path"])
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        summary = {}
    code = Path(record["test_path"]).read_text(encoding="utf-8", errors="replace")
    relative_path = _safe_relative_path(summary, record["instance_id"])
    return _new_file_patch(relative_path, code), summary, code


def execute_one(instance: dict, record: dict, args: argparse.Namespace) -> dict:
    destination = result_path(args.output_dir, record)
    if destination.is_file():
        try:
            previous = json.loads(destination.read_text(encoding="utf-8"))
            if previous.get("completed") and previous.get("sha256") == record["sha256"]:
                return {**previous, "resumed": True}
        except json.JSONDecodeError:
            pass
    patch, _summary, _code = make_patch(record)
    patch = remove_binary_diffs(patch)
    test_spec = make_test_spec(instance)
    spec = test_spec.exec_spec
    spec.timeout = args.timeout
    spec.rm_image = False
    spec.force_rebuild = False
    spec.run_id = args.run_id
    spec.compute_coverage = False
    spec.patch_id = f"checkpoint_{record['sha256'][:20]}"
    spec.test_directives = get_test_directives(patch, spec.repo)
    spec.patch_list = [patch]
    started = time.time()
    try:
        _instance_id, output = run_eval_exec_spec(spec, patch, build_mode="api")
        classification = classify_test_output(output, spec.repo)
        error = ""
    except Exception as exc:  # one Docker error is a recorded result, never a silent candidate
        output = None
        classification = {"category": "execution_missing", "patch_applied": False, "parsed_tests": {}, "evidence": str(exc)}
        error = f"{type(exc).__name__}: {exc}"
    payload = {
        **record,
        "completed": True,
        "resumed": False,
        "duration_seconds": round(time.time() - started, 3),
        "test_output_path": str(Path(output).resolve()) if output else "",
        "classification": classification,
        "error": error,
        "golden_or_fixed_used": False,
        "model_calls": 0,
    }
    atomic_json(destination, payload)
    return payload


def load_dataset(instance_ids: list[str], run_id: str) -> dict[str, dict]:
    predictions = {
        instance_id: {"instance_id": instance_id, "model_name_or_path": "checkpoint-revalidation", "model_patch": "placeholder"}
        for instance_id in instance_ids
    }
    rows = get_dataset_from_preds(
        "princeton-nlp/SWE-bench_Lite", "test", instance_ids, predictions,
        run_id, exclude_completed=False, filter_swt=True,
    )
    return {row["instance_id"]: row for row in rows}


def candidate_from_record(record: dict, summary: dict, code: str, ranking_dir: Path) -> CandidateTest:
    return CandidateTest(
        instance_id=record["instance_id"],
        round_id=int(record["round_id"]),
        code=code,
        candidate_file_path=str(ranking_dir / f"round_{record['round_id']}.py"),
        candidate_repo_path=str(summary.get("candidate_repo_path") or summary.get("direct_test_repo_path_hint") or ""),
        pytest_nodeid=str(summary.get("pytest_nodeid") or ""),
        command=str(summary.get("command") or ""),
    )


def select_best(manifest: dict, issues: dict[str, dict], related_code: dict, output_dir: Path) -> dict:
    grouped: dict[str, list[dict]] = {}
    for record in manifest["records"]:
        grouped.setdefault(record["instance_id"], []).append(record)
    selections = []
    selected_generation = output_dir / "selected_generation"
    selected_generation.mkdir(parents=True, exist_ok=True)
    for instance_id, records in sorted(grouped.items()):
        ranking_dir = output_dir / "ranking" / instance_id
        ranking_dir.mkdir(parents=True, exist_ok=True)
        source_dir = Path(records[0]["test_path"]).parent
        from brt6.issue.issue_rewriter import (
            apply_behavior_safety_constraints,
            apply_issue_authority_constraints,
            behavior_from_dict,
        )
        behavior = behavior_from_dict(
            instance_id,
            json.loads((source_dir / "behavior_target.json").read_text(encoding="utf-8")),
        )
        host = construct(HostContext, json.loads((source_dir / "host_context.json").read_text(encoding="utf-8")))
        issue_text = str(issues[instance_id].get("problem_statement") or "")
        behavior = apply_issue_authority_constraints(issue_text, behavior)
        behavior = apply_behavior_safety_constraints(issue_text, behavior)
        source_context = "\n\n".join(item.code_content for item in related_code.get(instance_id, []))
        checkpoints = []
        for record in sorted(records, key=lambda row: row["round_id"]):
            execution_payload = json.loads(result_path(output_dir, record).read_text(encoding="utf-8"))
            if not execution_payload.get("completed") or not execution_payload.get("test_output_path"):
                continue
            classification = execution_payload["classification"]
            output_text = Path(execution_payload["test_output_path"]).read_text(encoding="utf-8", errors="replace")
            status = CATEGORY_TO_STATUS[classification["category"]]
            execution = ExecutionResult(
                instance_id=instance_id,
                command="official SWTBench Docker buggy-side checkpoint revalidation",
                returncode=0 if status == "PASS" else 1,
                stdout=output_text,
                status=status,
                timeout=status == "TIMEOUT",
            )
            patch, summary, code = make_patch(record)
            del patch
            candidate = candidate_from_record(record, summary, code, ranking_dir)
            decision = verify_buggy_only(
                issue_text, behavior, candidate, execution, None,
                host.to_dict(), source_context, AblationConfig(),
            )
            checkpoint = _save_checkpoint(
                str(ranking_dir), int(record["round_id"]), candidate, execution,
                decision, None, behavior, issue_text, strict_result=None,
            )
            checkpoints.append((tuple(checkpoint.rank_key), record, checkpoint, summary, code))
        if not checkpoints:
            raise RuntimeError(f"no truly executed checkpoint is selectable for {instance_id}")
        _rank, selected, checkpoint, summary, code = max(checkpoints, key=lambda item: item[0])
        target = selected_generation / instance_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "final_test.py").write_text(code, encoding="utf-8")
        selected_summary = dict(summary)
        selected_summary.update({
            "checkpoint_selection": "brt5_rank_key_buggy_verified_only",
            "selected_round": selected["round_id"],
            "selected_sha256": selected["sha256"],
            "selected_rank_key": checkpoint.rank_key,
            "golden_or_fixed_used_for_selection": False,
            "model_calls": 0,
        })
        atomic_json(target / "summary.json", selected_summary)
        current = max(records, key=lambda row: row["round_id"])
        selections.append({
            "instance_id": instance_id,
            "selected_round": selected["round_id"],
            "selected_sha256": selected["sha256"],
            "current_round3_sha256": current["sha256"],
            "changed_from_current_final": selected["sha256"] != current["sha256"],
            "rank_key": checkpoint.rank_key,
            "verified_checkpoint_count": len(checkpoints),
        })
    summary = {
        "instances": len(selections),
        "changed_from_current_final": sum(row["changed_from_current_final"] for row in selections),
        "selections": selections,
        "golden_or_fixed_used_for_selection": False,
        "model_calls": 0,
    }
    atomic_json(output_dir / "selection_summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances", type=Path, required=True)
    parser.add_argument("--code-retrieval", type=Path, required=True)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--iterative-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=3000)
    parser.add_argument("--instance-ids", type=Path)
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--select-only", action="store_true")
    parser.add_argument("--expect-checkpoints", type=int)
    parser.add_argument("--expect-unique", type=int)
    args = parser.parse_args()
    for name in (
        "instances", "code_retrieval", "source_run", "iterative_run",
        "output_dir", "instance_ids",
    ):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.expanduser().resolve())
    if os.environ.get("BRT_API_POOL_FILE") or os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"):
        # Keys may exist in the controller environment, but this process must
        # never instantiate a client or perform a model request.
        os.environ["BRT_CHECKPOINT_REVALIDATION_ZERO_LLM"] = "1"
    issues_list = json.loads(args.instances.read_text(encoding="utf-8"))
    if args.instance_ids:
        allowed = {value.strip() for value in args.instance_ids.read_text(encoding="utf-8").splitlines() if value.strip()}
        issues_list = [row for row in issues_list if row["instance_id"] in allowed]
    issues = {row["instance_id"]: row for row in issues_list}
    roots = source_generations(args.source_run, args.iterative_run)
    manifest = build_manifest(sorted(issues), roots)
    if args.expect_checkpoints is not None and manifest["checkpoint_count"] != args.expect_checkpoints:
        raise RuntimeError(
            f"checkpoint gate failed: expected {args.expect_checkpoints}, "
            f"found {manifest['checkpoint_count']}"
        )
    if args.expect_unique is not None and manifest["unique_candidate_count"] != args.expect_unique:
        raise RuntimeError(
            f"unique-candidate gate failed: expected {args.expect_unique}, "
            f"found {manifest['unique_candidate_count']}"
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(args.output_dir / "manifest.json", manifest)
    if args.manifest_only:
        print(json.dumps({key: manifest[key] for key in ("checkpoint_count", "unique_candidate_count", "duplicate_count")}))
        return 0

    workspace = args.output_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    official_dataset = Path(__file__).resolve().parents[2] / "swt-bench" / "dataset"
    dataset_link = workspace / "dataset"
    if not dataset_link.exists():
        dataset_link.symlink_to(official_dataset, target_is_directory=True)
    os.chdir(workspace)
    if not args.select_only:
        install_retryable_source_fetches()
        apply_environment_compatibility()
        install_retryable_repo_setup()
        install_quiet_eval_diagnostics(skip_reinstall=True)
        install_tolerant_docker_output()
        dataset = load_dataset(sorted(issues), args.run_id)
        results = []
        with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            futures = {
                pool.submit(execute_one, dataset[row["instance_id"]], row, args): row
                for row in manifest["unique_candidates"]
            }
            for future in as_completed(futures):
                results.append(future.result())
        atomic_json(args.output_dir / "execution_summary.json", {
            "unique_candidates": len(results),
            "categories": dict(Counter(row["classification"]["category"] for row in results)),
            "resumed": sum(row.get("resumed", False) for row in results),
            "model_calls": 0,
            "golden_or_fixed_used": False,
        })

    related_code = load_code(args.code_retrieval)
    selection = select_best(manifest, issues, related_code, args.output_dir)
    print(json.dumps({"manifest": {key: manifest[key] for key in ("checkpoint_count", "unique_candidate_count", "duplicate_count")}, "selection": {key: selection[key] for key in ("instances", "changed_from_current_final")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
