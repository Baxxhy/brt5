#!/usr/bin/env python3
"""Post-hoc Golden Oracle Audit for an already completed BRT6 SWT run.

This program never imports an LLM client and never changes generation or
selection.  It inventories immutable test candidates, evaluates each unique
``(instance_id, sha256)`` candidate on the official buggy and golden-patched
SWT-Bench sides, and writes an analysis-only oracle portfolio.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import re
import tempfile
import time
from typing import Any

from src.auxillary_src.extract_patches import remove_binary_diffs
from src.constants import FAIL_TO_PASS
from src.dataset import get_dataset_from_preds
from src.grading import get_eval_report, get_logs_eval
from src.run_evaluation import run_eval_exec_spec
from src.test_spec import make_test_spec
from src.utils import get_test_directives

from export_swt_predictions import _new_file_patch, _safe_relative_path
from revalidate_swt_checkpoints import classify_test_output
from run_swt_official_env_compat import (
    apply_environment_compatibility,
    install_quiet_eval_diagnostics,
    install_retryable_repo_setup,
    install_retryable_source_fetches,
    install_tolerant_docker_output,
)


SOURCE_ORDER = {
    "Round0": 0,
    "Round1": 1,
    "Round2": 2,
    "Round3": 3,
    "Seed1": 4,
    "Seed2": 5,
    "Direct": 6,
}
INFRA_CATEGORIES = {
    "execution_missing",
    "patch_apply_error",
    "timeout",
    "zero_test",
    "syntax_error",
    "import_error",
    "collection_error",
    "setup_error",
}


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def atomic_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_roots(run_dir: Path) -> list[dict]:
    return [
        {"source": "Round0", "round": 0, "seed_index": 0, "root": run_dir / "generation"},
        {"source": "Round1", "round": 1, "seed_index": 0, "root": run_dir / "feedback/round_1/generation"},
        {"source": "Round2", "round": 2, "seed_index": 0, "root": run_dir / "feedback/round_2/generation"},
        {"source": "Round3", "round": 3, "seed_index": 0, "root": run_dir / "feedback/round_3/generation"},
        {"source": "Seed1", "round": 0, "seed_index": 1, "root": run_dir / "feedback/adaptive_seeds/seed_1/generation"},
        {"source": "Seed2", "round": 0, "seed_index": 2, "root": run_dir / "feedback/adaptive_seeds/seed_2/generation"},
        {"source": "Direct", "round": 0, "seed_index": None, "root": run_dir / "generation_direct"},
    ]


def artifact_source(origin: dict, filename: str) -> tuple[str, int, int | None, str]:
    if filename == "final_test.py":
        return origin["source"], origin["round"], origin["seed_index"], "stage_final"
    if origin["source"] in {"Seed1", "Seed2", "Direct"}:
        role = "checkpoint" if filename.startswith("candidate_round_") else "mutation_intermediate"
        return origin["source"], origin["round"], origin["seed_index"], role
    if filename.startswith("candidate_round_"):
        try:
            round_id = int(filename.split("candidate_round_", 1)[1].split("_", 1)[0].split(".", 1)[0])
        except ValueError:
            round_id = origin["round"]
        role = "rejected_oracle_checkpoint" if "oracle_violation" in filename else "checkpoint"
        return f"Round{round_id}", round_id, 0, role
    if filename.startswith("mutation_round_"):
        return "Round0", 0, 0, "mutation_intermediate"
    return origin["source"], origin["round"], origin["seed_index"], "other_candidate"


def load_evidence_maps(run_dir: Path) -> dict[str, dict[str, dict]]:
    paths = {
        "Round0": run_dir / "feedback/round_1/generation/docker_feedback_summary.json",
        "Round1": run_dir / "feedback/round_2/generation/docker_feedback_summary.json",
        "Round2": run_dir / "feedback/round_3/generation/docker_feedback_summary.json",
        "Round3": run_dir / "feedback/final_verification/verifier/docker_feedback_summary.json",
        "Seed1": run_dir / "feedback/adaptive_seeds/seed_1/verifier/docker_feedback_summary.json",
        "Seed2": run_dir / "feedback/adaptive_seeds/seed_2/verifier/docker_feedback_summary.json",
        "Direct": run_dir / "feedback/issue_only_portfolio/direct/buggy_execution_summary.json",
    }
    result: dict[str, dict[str, dict]] = {}
    for source, path in paths.items():
        payload = read_json(path, {}) or {}
        result[source] = {
            row["instance_id"]: row
            for row in payload.get("results", [])
            if isinstance(row, dict) and row.get("instance_id")
        }
    surrogate_paths = {
        "Primary": run_dir / "feedback/issue_only_portfolio/primary/surrogate/surrogate_execution_summary.json",
        "Direct": run_dir / "feedback/issue_only_portfolio/direct/surrogate/surrogate_execution_summary.json",
    }
    for source, path in surrogate_paths.items():
        payload = read_json(path, {}) or {}
        result[f"{source}Surrogate"] = {
            row["instance_id"]: row
            for row in payload.get("results", [])
            if isinstance(row, dict) and row.get("instance_id")
        }
    return result


def compact_evidence(value: dict | None) -> dict:
    if not value:
        return {}
    decision = value.get("decision") if isinstance(value.get("decision"), dict) else {}
    return {
        key: item
        for key, item in {
            "action": value.get("action"),
            "status": value.get("status") or value.get("surrogate_status"),
            "verified_candidate": value.get("verified_candidate"),
            "surrogate_pass": value.get("surrogate_pass"),
            "candidate_sha256": value.get("candidate_sha256"),
            "decision": decision.get("decision"),
            "reason": decision.get("reason") or value.get("reason"),
            "focus": decision.get("focus") or value.get("focus"),
        }.items()
        if item not in (None, "", [])
    }


def build_manifest(run_dir: Path, instance_ids: set[str]) -> dict:
    final_selection = read_json(run_dir / "evaluation/verified_fallback_selection.json", {}) or {}
    selected = {
        row["instance_id"]: row
        for row in final_selection.get("selections", [])
        if row.get("instance_id")
    }
    evidence_maps = load_evidence_maps(run_dir)
    candidates: dict[tuple[str, str], dict] = {}
    artifact_count = 0
    for origin in source_roots(run_dir):
        root = origin["root"]
        if not root.is_dir():
            continue
        for instance_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            instance_id = instance_dir.name
            if instance_id not in instance_ids:
                continue
            summary_path = instance_dir / "summary.json"
            summary = read_json(summary_path, {}) or {}
            for candidate_path in sorted(instance_dir.glob("*.py")):
                artifact_count += 1
                code = candidate_path.read_text(encoding="utf-8", errors="replace")
                digest = sha256_text(code)
                source, round_id, seed_index, role = artifact_source(origin, candidate_path.name)
                provenance = {
                    "source": source,
                    "round": round_id,
                    "seed_index": seed_index,
                    "artifact_role": role,
                    "artifact_name": candidate_path.name,
                    "candidate_path": str(candidate_path.resolve()),
                    "summary_path": str(summary_path.resolve()),
                    "stage_root": str(root.resolve()),
                }
                key = (instance_id, digest)
                row = candidates.setdefault(key, {
                    "instance_id": instance_id,
                    "candidate_id": f"{instance_id}__{digest[:20]}",
                    "sha256": digest,
                    "candidate_path": str(candidate_path.resolve()),
                    "summary_path": str(summary_path.resolve()),
                    "source": source,
                    "round": round_id,
                    "seed_index": seed_index,
                    "sources": [],
                    "provenance": [],
                })
                if source not in row["sources"]:
                    row["sources"].append(source)
                if provenance not in row["provenance"]:
                    row["provenance"].append(provenance)
                current_key = (SOURCE_ORDER.get(row["source"], 99), row["round"] or 0)
                proposed_key = (SOURCE_ORDER.get(source, 99), round_id or 0)
                if proposed_key < current_key:
                    row.update({
                        "candidate_path": str(candidate_path.resolve()),
                        "summary_path": str(summary_path.resolve()),
                        "source": source,
                        "round": round_id,
                        "seed_index": seed_index,
                    })
    for row in candidates.values():
        row["sources"].sort(key=lambda item: SOURCE_ORDER.get(item, 99))
        row["provenance"].sort(key=lambda item: (
            SOURCE_ORDER.get(item["source"], 99), item["artifact_name"], item["candidate_path"],
        ))
        chosen = selected.get(row["instance_id"], {})
        row["selected_by_brt6"] = chosen.get("selected_sha256") == row["sha256"]
        row["selection_record"] = chosen if row["selected_by_brt6"] else {}
        row["buggy_side_evidence"] = {
            source: compact_evidence(evidence_maps.get(source, {}).get(row["instance_id"]))
            for source in row["sources"]
            if evidence_maps.get(source, {}).get(row["instance_id"])
        }
        direct_surrogate = evidence_maps.get("DirectSurrogate", {}).get(row["instance_id"])
        if "Direct" in row["sources"] and direct_surrogate:
            row["surrogate_evidence"] = compact_evidence(direct_surrogate)
        elif row["selected_by_brt6"] and chosen.get("selected_label") == "primary":
            row["surrogate_evidence"] = compact_evidence(
                evidence_maps.get("PrimarySurrogate", {}).get(row["instance_id"])
            )
        else:
            row["surrogate_evidence"] = {}
    rows = sorted(candidates.values(), key=lambda row: (row["instance_id"], row["sha256"]))
    selected_missing = sorted(
        instance_id
        for instance_id, choice in selected.items()
        if (instance_id, choice.get("selected_sha256", "")) not in candidates
    )
    return {
        "total_instances": len(instance_ids),
        "artifact_files": artifact_count,
        "unique_candidate_count": len(rows),
        "duplicate_artifact_count": artifact_count - len(rows),
        "selected_candidates_found": sum(row["selected_by_brt6"] for row in rows),
        "selected_candidates_missing": selected_missing,
        "candidates": rows,
    }


def load_predictions(path: Path) -> dict[str, dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {row["instance_id"]: row for row in rows}


def side_result(output_path: Path, repo: str) -> dict:
    try:
        classification = classify_test_output(output_path, repo)
        parsed, patch_applied = get_logs_eval(str(output_path), repo, "unit_test")
    except IndexError:
        # The pinned official parser recognizes ``+ python3 /root/trace.py``
        # but Sphinx tox prints the semantically identical interpreter as
        # ``+ .tox/<env>/bin/python3 /root/trace.py``.  Normalize only that
        # command prefix in a temporary parser input; the immutable execution
        # log, test statuses, patch markers, and grading code remain unchanged.
        raw = output_path.read_text(encoding="utf-8", errors="replace")
        normalized = re.sub(
            r"(?m)^\+\s+\S*/python3\s+(?=/root/trace\.py\s+--count\s+-C\s+coverage\.cover)",
            "+ python3 ",
            raw,
        )
        if normalized == raw:
            raise
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".txt", delete=False,
                dir=output_path.parent,
            ) as temporary:
                temporary.write(normalized)
                temporary_name = temporary.name
            normalized_path = Path(temporary_name)
            classification = classify_test_output(normalized_path, repo)
            parsed, patch_applied = get_logs_eval(str(normalized_path), repo, "unit_test")
            classification["parser_compatibility"] = "tox_python3_absolute_path_normalized"
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
    values = list(parsed.values())
    if not patch_applied or classification["category"] in INFRA_CATEGORIES:
        status = "ERROR"
    elif any(value in {"FAILED", "ERROR", "ERROR:"} for value in values):
        status = "FAIL"
    elif any(value == "PASSED" for value in values):
        status = "PASS"
    elif classification["category"] == "pass":
        status = "PASS"
    elif classification["category"] in {"assertion_failure", "runtime_error"}:
        status = "FAIL"
    else:
        status = "ERROR"
    return {
        "status": status,
        "category": classification["category"],
        "patch_applied": patch_applied,
        "parsed_tests": dict(sorted(parsed.items())),
        "classification": classification,
        "output_path": str(output_path.resolve()),
    }


def combine_sides(buggy: dict, fixed: dict) -> dict:
    report = get_eval_report(buggy["parsed_tests"], fixed["parsed_tests"])
    added_f2p = len(report[FAIL_TO_PASS])
    is_f2p = added_f2p > 0
    if buggy["status"] == "ERROR":
        error_type = buggy["category"]
    elif fixed["status"] == "ERROR":
        error_type = fixed["category"]
    elif is_f2p:
        error_type = "" if fixed["status"] == "PASS" else "f2p_with_residual_fixed_failure"
    elif buggy["status"] == "PASS":
        error_type = "buggy_pass"
    elif buggy["status"] == "FAIL" and fixed["status"] == "FAIL":
        error_type = "f2f"
    else:
        error_type = "unmatched_transition"
    return {
        "buggy_status": buggy["status"],
        "fixed_status": fixed["status"],
        "is_f2p": is_f2p,
        "added_f2p": added_f2p,
        "execution_status": f"buggy_{buggy['status']}__fixed_{fixed['status']}",
        "error_type": error_type,
        "buggy_execution": buggy,
        "fixed_execution": fixed,
    }


def consistency_check(run_dir: Path, manifest: dict) -> dict:
    predictions_path = run_dir / "evaluation/predictions.jsonl"
    metrics = read_json(run_dir / "evaluation/metrics.json", {}) or {}
    selection = read_json(run_dir / "evaluation/verified_fallback_selection.json", {}) or {}
    predictions = load_predictions(predictions_path)
    choices = {row["instance_id"]: row for row in selection.get("selections", [])}
    selected_rows = {row["instance_id"]: row for row in manifest["candidates"] if row["selected_by_brt6"]}
    expected_ids = set(choices)
    problems = []
    calculated_f2p = []
    model_name = str(metrics.get("model_name") or "brt6__DeepSeek-V4-Flash")
    run_id = str(metrics.get("run_id") or run_dir.name)
    log_root = run_dir / "official_workspace/run_instance_swt_logs" / run_id
    for instance_id in sorted(expected_ids):
        candidate = selected_rows.get(instance_id)
        prediction = predictions.get(instance_id)
        if not candidate or not prediction:
            problems.append(f"{instance_id}: missing selected candidate or prediction")
            continue
        code = Path(candidate["candidate_path"]).read_text(encoding="utf-8", errors="replace")
        if sha256_text(code) != choices[instance_id].get("selected_sha256"):
            problems.append(f"{instance_id}: selected SHA mismatch")
        summary = read_json(Path(candidate["summary_path"]), {}) or {}
        expected_patch = _new_file_patch(_safe_relative_path(summary, instance_id), code)
        if remove_binary_diffs(prediction.get("model_patch", "")) != remove_binary_diffs(expected_patch):
            problems.append(f"{instance_id}: exported prediction patch differs from selected candidate")
        pre = log_root / f"pred_pre__{model_name}" / instance_id / "test_output.txt"
        post = log_root / f"pred_post__{model_name}" / instance_id / "test_output.txt"
        if not pre.is_file() or not post.is_file():
            problems.append(f"{instance_id}: missing official pre/post output")
            continue
        repo = instance_id.rsplit("-", 1)[0].replace("__", "/")
        combined = combine_sides(side_result(pre, repo), side_result(post, repo))
        if combined["is_f2p"]:
            calculated_f2p.append(instance_id)
    official_f2p = sorted(metrics.get("f2p_success_ids", []))
    if sorted(calculated_f2p) != official_f2p:
        missing = sorted(set(official_f2p) - set(calculated_f2p))
        extra = sorted(set(calculated_f2p) - set(official_f2p))
        problems.append(f"F2P parser mismatch: missing={missing}, extra={extra}")
    result = {
        "prediction_count": len(predictions),
        "selection_count": len(choices),
        "selected_candidates_found": len(selected_rows),
        "official_selected_f2p": metrics.get("f2p_success_instances"),
        "reparsed_selected_f2p": len(calculated_f2p),
        "official_error_instances": metrics.get("error_instances"),
        "pipeline_consistent": not problems,
        "problems": problems,
    }
    if len(predictions) != 276 or len(choices) != 276 or len(selected_rows) != 276:
        result["pipeline_consistent"] = False
        result["problems"].append("expected exactly 276 predictions, selections, and selected candidates")
    if metrics.get("f2p_success_instances") != 132:
        result["pipeline_consistent"] = False
        result["problems"].append("formal selected F2P is not the frozen 132 baseline")
    return result


def load_dataset(instance_ids: list[str], run_id: str) -> dict[str, dict]:
    predictions = {
        instance_id: {
            "instance_id": instance_id,
            "model_name_or_path": "golden-oracle-audit",
            "model_patch": "placeholder",
        }
        for instance_id in instance_ids
    }
    rows = get_dataset_from_preds(
        "princeton-nlp/SWE-bench_Lite",
        "test",
        instance_ids,
        predictions,
        run_id,
        exclude_completed=False,
        filter_swt=True,
    )
    return {row["instance_id"]: row for row in rows}


def cache_path(output_dir: Path, candidate: dict) -> Path:
    return output_dir / "cache" / candidate["instance_id"] / f"{candidate['sha256']}.json"


def parse_frozen_selected(run_dir: Path, metrics: dict, candidate: dict) -> dict:
    instance_id = candidate["instance_id"]
    model_name = str(metrics["model_name"])
    log_root = run_dir / "official_workspace/run_instance_swt_logs" / metrics["run_id"]
    pre = log_root / f"pred_pre__{model_name}" / instance_id / "test_output.txt"
    post = log_root / f"pred_post__{model_name}" / instance_id / "test_output.txt"
    repo = instance_id.rsplit("-", 1)[0].replace("__", "/")
    result = combine_sides(side_result(pre, repo), side_result(post, repo))
    result.update({
        "completed": True,
        "candidate_id": candidate["candidate_id"],
        "instance_id": instance_id,
        "sha256": candidate["sha256"],
        "duration_seconds": 0.0,
        "cache_source": "frozen_official_selected_run",
        "model_calls": 0,
        "golden_used_for_analysis_only": True,
    })
    return result


def execute_candidate(instance: dict, candidate: dict, args: argparse.Namespace) -> dict:
    destination = cache_path(args.output_dir, candidate)
    previous = read_json(destination, {}) or {}
    if previous.get("completed") and previous.get("sha256") == candidate["sha256"]:
        return {**previous, "resumed": True}
    metrics = read_json(args.run_dir / "evaluation/metrics.json", {}) or {}
    if candidate["selected_by_brt6"]:
        result = parse_frozen_selected(args.run_dir, metrics, candidate)
        atomic_json(destination, result)
        return result

    instance_id = candidate["instance_id"]
    code = Path(candidate["candidate_path"]).read_text(encoding="utf-8", errors="replace")
    summary = read_json(Path(candidate["summary_path"]), {}) or {}
    patch = remove_binary_diffs(
        _new_file_patch(_safe_relative_path(summary, instance_id), code)
    )
    test_spec = make_test_spec(instance)
    spec = test_spec.exec_spec
    spec.timeout = args.timeout
    spec.rm_image = False
    spec.force_rebuild = False
    spec.run_id = args.run_id
    spec.compute_coverage = True
    spec.test_directives = get_test_directives(patch, spec.repo)
    started = time.time()
    outputs: dict[str, Path] = {}
    side_errors: dict[str, str] = {}
    for side, code_patch in (("buggy", None), ("fixed", test_spec.golden_code_patch)):
        try:
            spec.patch_id = f"oracle_{side}_{candidate['sha256'][:20]}"
            spec.patch_list = ([] if code_patch is None else [code_patch]) + [patch]
            _instance_id, output = run_eval_exec_spec(spec, patch, build_mode="api")
            outputs[side] = Path(output)
        except Exception as exc:
            side_errors[side] = f"{type(exc).__name__}: {exc}"

    def load_side(side: str) -> dict:
        if side in outputs:
            return side_result(outputs[side], spec.repo)
        error = side_errors.get(side, "execution output missing")
        return {
            "status": "ERROR",
            "category": "execution_missing",
            "patch_applied": False,
            "parsed_tests": {},
            "classification": {"category": "execution_missing", "evidence": error},
            "output_path": "",
        }

    result = combine_sides(load_side("buggy"), load_side("fixed"))
    error = "; ".join(f"{side}: {message}" for side, message in sorted(side_errors.items()))
    result.update({
        "completed": True,
        "candidate_id": candidate["candidate_id"],
        "instance_id": instance_id,
        "sha256": candidate["sha256"],
        "duration_seconds": round(time.time() - started, 3),
        "cache_source": "golden_oracle_audit",
        "resumed": False,
        "error": error,
        "model_calls": 0,
        "golden_used_for_analysis_only": True,
    })
    atomic_json(destination, result)
    return result


def public_candidate_result(candidate: dict, execution: dict) -> dict:
    error_type = execution["error_type"]
    if execution.get("is_f2p") and execution.get("fixed_status") == "FAIL":
        error_type = "f2p_with_residual_fixed_failure"
    return {
        "instance_id": candidate["instance_id"],
        "candidate_id": candidate["candidate_id"],
        "sha256": candidate["sha256"],
        "source": candidate["source"],
        "sources": candidate["sources"],
        "round": candidate["round"],
        "seed_index": candidate["seed_index"],
        "candidate_path": candidate["candidate_path"],
        "selected_by_brt6": candidate["selected_by_brt6"],
        "buggy_status": execution["buggy_status"],
        "fixed_status": execution["fixed_status"],
        "is_f2p": execution["is_f2p"],
        "execution_status": execution["execution_status"],
        "error_type": error_type,
        "provenance": candidate["provenance"],
        "buggy_side_evidence": candidate.get("buggy_side_evidence", {}),
        "surrogate_evidence": candidate.get("surrogate_evidence", {}),
        "selection_record": candidate.get("selection_record", {}),
        "added_f2p": execution.get("added_f2p", 0),
        "duration_seconds": execution.get("duration_seconds", 0),
        "cache_source": execution.get("cache_source", ""),
        "buggy_execution": execution.get("buggy_execution", {}),
        "fixed_execution": execution.get("fixed_execution", {}),
    }


def generation_miss_type(rows: list[dict]) -> str:
    if rows and all(row["buggy_status"] == "PASS" for row in rows):
        return "BUGGY_PASS_target_behavior_not_triggered"
    errors = {row["error_type"] for row in rows}
    if "timeout" in errors:
        return "timeout"
    if errors & {"syntax_error", "import_error", "collection_error", "setup_error", "zero_test", "patch_apply_error", "execution_missing"}:
        return "syntax_setup_import_or_collection"
    f2f = [row for row in rows if row["buggy_status"] == "FAIL" and row["fixed_status"] == "FAIL"]
    if f2f:
        evidence_text = json.dumps(
            [row.get("buggy_side_evidence", {}) for row in f2f], ensure_ascii=False
        ).lower()
        if "trigger" in evidence_text or "输入" in evidence_text or "状态" in evidence_text:
            return "F2F_wrong_input_state_or_trigger"
        if "assert" in evidence_text or "断言" in evidence_text:
            return "F2F_wrong_assertion_or_oracle"
        return "F2F"
    if any(row["buggy_status"] == "PASS" for row in rows):
        return "target_behavior_not_triggered"
    return "other"


def finalize(run_dir: Path, output_dir: Path, manifest: dict, allow_partial: bool) -> dict:
    rows = []
    missing = []
    for candidate in manifest["candidates"]:
        execution = read_json(cache_path(output_dir, candidate), {}) or {}
        if not execution.get("completed"):
            missing.append(candidate["candidate_id"])
            continue
        rows.append(public_candidate_result(candidate, execution))
    rows.sort(key=lambda row: (row["instance_id"], row["sha256"]))
    atomic_jsonl(output_dir / "candidate_results.jsonl", rows)
    if missing and not allow_partial:
        raise RuntimeError(f"audit incomplete: {len(missing)} candidate evaluations missing")

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["instance_id"]].append(row)
    per_instance = {}
    selection_misses = []
    generation_misses = []
    for instance_id in sorted(grouped):
        candidates = grouped[instance_id]
        selected = next((row for row in candidates if row["selected_by_brt6"]), None)
        f2p_candidates = [row for row in candidates if row["is_f2p"]]
        oracle = selected if selected and selected["is_f2p"] else (
            sorted(f2p_candidates, key=lambda row: (SOURCE_ORDER.get(row["source"], 99), row["sha256"]))[0]
            if f2p_candidates else None
        )
        missed = bool(f2p_candidates and (not selected or not selected["is_f2p"]))
        item = {
            "instance_id": instance_id,
            "current_selected_candidate": selected["candidate_id"] if selected else None,
            "current_selected_source": selected["source"] if selected else None,
            "current_selected_is_f2p": bool(selected and selected["is_f2p"]),
            "all_historical_candidates": [row["candidate_id"] for row in candidates],
            "candidate_count": len(candidates),
            "has_f2p_candidate": bool(f2p_candidates),
            "f2p_candidates": [row["candidate_id"] for row in f2p_candidates],
            "oracle_best_candidate": oracle["candidate_id"] if oracle else None,
            "oracle_best_source": oracle["source"] if oracle else None,
            "selector_missed_existing_f2p": missed,
        }
        if missed:
            comparison = {
                "instance_id": instance_id,
                "selected_candidate": selected,
                "oracle_f2p_candidate": oracle,
            }
            selection_misses.append(comparison)
        if not f2p_candidates:
            miss_type = generation_miss_type(candidates)
            item["generation_miss_type"] = miss_type
            generation_misses.append({"instance_id": instance_id, "failure_type": miss_type})
        per_instance[instance_id] = item
    atomic_json(output_dir / "per_instance_oracle.json", {
        "total_instances": len(per_instance),
        "instances": per_instance,
    })
    atomic_json(output_dir / "selection_misses.json", {
        "count": len(selection_misses),
        "instances": selection_misses,
    })
    atomic_json(output_dir / "generation_misses.json", {
        "count": len(generation_misses),
        "failure_types": dict(Counter(row["failure_type"] for row in generation_misses)),
        "instances": generation_misses,
    })

    metrics = read_json(run_dir / "evaluation/metrics.json", {}) or {}
    selected_f2p = sum(
        bool(item["current_selected_is_f2p"])
        for item in per_instance.values()
    )
    oracle_f2p = sum(bool(item["has_f2p_candidate"]) for item in per_instance.values())
    source_stats = {}
    for source in SOURCE_ORDER:
        source_rows = [row for row in rows if source in row["sources"]]
        f2p_rows = [row for row in source_rows if row["is_f2p"]]
        source_stats[source] = {
            "candidate_count": len(source_rows),
            "f2p_candidate_count": len(f2p_rows),
            "unique_f2p_instances": len({row["instance_id"] for row in f2p_rows}),
            "unique_f2p_instance_ids": sorted({row["instance_id"] for row in f2p_rows}),
        }
    denominator = 276
    summary = {
        "total_instances": denominator,
        "evaluated_unique_candidates": len(rows),
        "expected_unique_candidates": manifest["unique_candidate_count"],
        "missing_candidate_evaluations": len(missing),
        "selected_f2p": selected_f2p,
        "frozen_selected_f2p": metrics.get("f2p_success_instances"),
        "oracle_portfolio_f2p": oracle_f2p,
        "oracle_portfolio_rate": round(100.0 * oracle_f2p / denominator, 4),
        "selection_gap": oracle_f2p - selected_f2p,
        "instances_with_f2p_but_not_selected": len(selection_misses),
        "instances_with_f2p_but_not_selected_ids": [row["instance_id"] for row in selection_misses],
        "instances_with_no_f2p_candidate": len(generation_misses),
        "instances_with_no_f2p_candidate_ids": [row["instance_id"] for row in generation_misses],
        "selected_f2p_over_oracle_portfolio": (
            round(selected_f2p / oracle_f2p, 6) if oracle_f2p else 0.0
        ),
        "generation_miss": len(generation_misses),
        "generation_miss_failure_types": dict(Counter(row["failure_type"] for row in generation_misses)),
        "source_statistics": source_stats,
        "golden_used_for_analysis_only": True,
        "formal_selection_unchanged": True,
        "model_calls": 0,
    }
    atomic_json(output_dir / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--instances", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--max-workers", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=3000)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--consistency-only", action="store_true")
    parser.add_argument("--candidate-ids-file", type=Path)
    parser.add_argument("--smoke-count", type=int, default=0)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--finalize-only", action="store_true")
    args = parser.parse_args()
    for name in ("run_dir", "instances", "output_dir", "candidate_ids_file"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.expanduser().resolve())

    # Make accidental model access impossible even when the controller happens
    # to inherit credentials from a generation run.
    for name in ("BRT_API_POOL_FILE", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        os.environ.pop(name, None)
    os.environ["BRT_GOLDEN_ORACLE_AUDIT_ZERO_LLM"] = "1"

    issues_list = read_json(args.instances, []) or []
    instance_ids = {row["instance_id"] for row in issues_list}
    if len(instance_ids) != 276:
        raise RuntimeError(f"expected SWT-Lite 276 instances, found {len(instance_ids)}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(args.run_dir, instance_ids)
    atomic_json(args.output_dir / "candidate_manifest.json", manifest)
    print(json.dumps({
        "artifact_files": manifest["artifact_files"],
        "unique_candidates": manifest["unique_candidate_count"],
        "selected_found": manifest["selected_candidates_found"],
    }))
    if manifest["selected_candidates_missing"]:
        raise RuntimeError(
            f"selected candidates absent from inventory: {manifest['selected_candidates_missing']}"
        )
    if args.inventory_only:
        return 0

    consistency = consistency_check(args.run_dir, manifest)
    atomic_json(args.output_dir / "consistency.json", consistency)
    print(json.dumps(consistency))
    if not consistency["pipeline_consistent"]:
        raise RuntimeError("frozen official evaluation consistency gate failed")
    if args.consistency_only:
        return 0

    workspace = args.output_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    official_dataset = Path(__file__).resolve().parents[2] / "swt-bench" / "dataset"
    dataset_link = workspace / "dataset"
    if not dataset_link.exists():
        dataset_link.symlink_to(official_dataset, target_is_directory=True)
    os.chdir(workspace)

    candidates = manifest["candidates"]
    if args.candidate_ids_file:
        wanted = {
            line.strip()
            for line in args.candidate_ids_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        candidates = [row for row in candidates if row["candidate_id"] in wanted]
        missing_ids = sorted(wanted - {row["candidate_id"] for row in candidates})
        if missing_ids:
            raise RuntimeError(f"unknown candidate IDs: {missing_ids}")
    if args.smoke_count:
        chosen = []
        used_sources = set()
        used_repos = set()
        for row in sorted(candidates, key=lambda item: (
            item["selected_by_brt6"],
            SOURCE_ORDER.get(item["source"], 99),
            item["instance_id"],
            item["sha256"],
        )):
            repo = row["instance_id"].rsplit("-", 1)[0]
            if row["source"] in used_sources or repo in used_repos:
                continue
            chosen.append(row)
            used_sources.add(row["source"])
            used_repos.add(repo)
            if len(chosen) >= args.smoke_count:
                break
        for row in sorted(candidates, key=lambda item: (
            item["selected_by_brt6"], item["instance_id"], item["sha256"],
        )):
            if len(chosen) >= args.smoke_count:
                break
            if row not in chosen:
                chosen.append(row)
        candidates = chosen

    if not args.finalize_only:
        install_retryable_source_fetches()
        apply_environment_compatibility()
        install_retryable_repo_setup()
        install_quiet_eval_diagnostics(skip_reinstall=True)
        install_tolerant_docker_output()
        dataset = load_dataset(sorted({row["instance_id"] for row in candidates}), args.run_id)
        completed = 0
        scheduler_errors = []
        with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            futures = {
                pool.submit(execute_candidate, dataset[row["instance_id"]], row, args): row
                for row in candidates
            }
            for future in as_completed(futures):
                row = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    scheduler_errors.append({
                        "candidate_id": row["candidate_id"],
                        "instance_id": row["instance_id"],
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    print(json.dumps({"candidate_id": row["candidate_id"], "scheduler_error": scheduler_errors[-1]["error"]}))
                    continue
                completed += 1
                print(json.dumps({
                    "completed": completed,
                    "total": len(candidates),
                    "candidate_id": row["candidate_id"],
                    "buggy": result["buggy_status"],
                    "fixed": result["fixed_status"],
                    "is_f2p": result["is_f2p"],
                    "resumed": result.get("resumed", False),
                }))
        atomic_json(args.output_dir / "scheduler_errors.json", {
            "count": len(scheduler_errors),
            "errors": scheduler_errors,
        })
        if scheduler_errors:
            raise RuntimeError(
                f"{len(scheduler_errors)} candidate workers failed before producing cache records"
            )

    summary = finalize(
        args.run_dir,
        args.output_dir,
        manifest,
        allow_partial=args.allow_partial or bool(args.candidate_ids_file) or bool(args.smoke_count),
    )
    if args.smoke_count:
        atomic_json(args.output_dir / "smoke_summary.json", {
            "requested": args.smoke_count,
            "candidate_ids": [row["candidate_id"] for row in candidates],
            "sources": [row["source"] for row in candidates],
            "instances": [row["instance_id"] for row in candidates],
            "partial_summary": summary,
        })
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
