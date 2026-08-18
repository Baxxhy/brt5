#!/usr/bin/env python3
"""Build a per-instance attribution report from official SWT-Bench outputs."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


TRANSITIONS = ("FAIL_TO_PASS", "PASS_TO_PASS", "FAIL_TO_FAIL", "PASS_TO_FAIL", "UNMATCHED")


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def read_phase(root: Path, run_id: str, phase: str, instance_id: str) -> tuple[str, str]:
    matches = sorted((root / run_id).glob(f"{phase}__*/{instance_id}/test_output.txt"))
    if not matches:
        return "", ""
    return matches[-1].read_text(encoding="utf-8", errors="replace"), str(matches[-1])


def read_reference_phase(root: Path, phase: str, instance_id: str) -> tuple[str, str]:
    matches = sorted((root / phase / instance_id).glob("*/test_output.txt"))
    if not matches:
        return "", ""
    return matches[-1].read_text(encoding="utf-8", errors="replace"), str(matches[-1])


def detail_class(text: str) -> str:
    lowered = text.lower()
    if not text:
        return "missing_execution_output"
    if "syntaxerror" in lowered or "indentationerror" in lowered:
        return "syntax_or_indentation_error"
    if "requires pytest-" in lowered or "actual pytest-" in lowered or "unknown config option" in lowered or "unrecognized arguments" in lowered:
        return "runner_or_dependency_incompatibility"
    if "has no migration class" in lowered or "badmigrationerror" in lowered:
        return "generated_artifact_conflicts_with_repo_conventions"
    if any(x in lowered for x in ("importerror", "modulenotfounderror", "_failedtest", "error collecting")):
        return "import_or_collection_error"
    if any(x in lowered for x in ("not found:", "file or directory not found", "has no attribute")):
        return "test_selector_or_target_not_found"
    if re.search(r"\b(0 tests|no tests ran|ran 0 tests|collected 0 items)\b", lowered):
        return "zero_tests_collected"
    if "assertionerror" in lowered or re.search(r"^e\s+assert\b", lowered, re.MULTILINE):
        return "assertion_or_oracle_failure"
    if re.search(r"(?:error|exception|failed)", lowered):
        return "runtime_or_setup_failure"
    return "official_parser_unmatched_output"


def evidence(text: str) -> str:
    candidates = []
    for raw in text.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", raw).strip()
        if not line or len(line) > 600:
            continue
        score = 0
        lowered = line.lower()
        if re.match(r"^(?:e\s+)?[\w.]+(?:error|exception)(?::|$)", lowered):
            score += 12
        if re.match(r"^(?:e\s+)?[\w.]+(?:404|exit)(?::|$)", lowered):
            score += 10
        if re.search(r"(?:syntax|indentation|import|module|assertion|migration|collection|config).*error", lowered):
            score += 8
        if re.search(r"(?:error|exception|failed|requires pytest|not found|no tests ran|ran 0 tests)", lowered):
            score += 5
        if line.startswith(("E ", "ERROR", "FAILED", "ImportError", "SyntaxError", "AssertionError")):
            score += 4
        if "exec_tests" in lowered or "coverage" in lowered or "docker" in lowered:
            score -= 3
        if score > 0:
            candidates.append((score, len(candidates), line))
    if not candidates:
        return "No concise exception line found; inspect the linked official output."
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2][:500]


def primary_category(report: dict) -> str:
    pred = report["tests_pred"]
    if report.get("resolved") or report.get("added_f2p", 0) > 0:
        return "success_f2p"
    if pred["PASS_TO_FAIL"]:
        return "wrong_direction_pass_to_fail"
    if pred["UNMATCHED"]:
        return "invalid_or_unparsed_execution"
    if pred["FAIL_TO_FAIL"]:
        return "fails_both_wrong_oracle_or_shared_failure"
    if pred["PASS_TO_PASS"]:
        return "passes_both_bug_not_triggered"
    return "no_parsed_test_transition"


def parsed_transition_count(transitions: dict) -> int:
    return sum(len(transitions.get(key, [])) for key in TRANSITIONS if key != "UNMATCHED")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    run = args.run_dir.resolve()
    run_id = run.name
    official = run / "official_workspace" / "run_instance_swt_logs"
    report_paths = sorted((official / run_id).glob("*/*/report.json"))
    if not report_paths:
        raise SystemExit(f"No official reports found below {official / run_id}")

    feedback = {}
    for round_no in (1, 2, 3):
        path = run / "feedback" / f"round_{round_no}" / "generation" / "docker_feedback_summary.json"
        if path.exists():
            for item in load_json(path).get("results", []):
                feedback.setdefault(item["instance_id"], {})[str(round_no)] = {
                    key: item.get(key) for key in ("action", "focus", "status") if item.get(key) is not None
                }

    records = []
    for report_path in report_paths:
        payload = load_json(report_path)
        instance_id, report = next(iter(payload.items()))
        pre, pre_path = read_phase(official, run_id, "pred_pre", instance_id)
        post, post_path = read_phase(official, run_id, "pred_post", instance_id)
        gold_pre, gold_pre_path = read_reference_phase(official, "gold_pre", instance_id)
        gold_post, gold_post_path = read_reference_phase(official, "gold_post", instance_id)
        category = primary_category(report)
        gold_parsed = parsed_transition_count(report["tests_gold"])
        evaluation_valid = bool(report.get("resolved")) or gold_parsed > 0
        root_attribution = (
            "success_f2p" if report.get("resolved")
            else "evaluation_environment_invalid_reference" if not evaluation_valid
            else category
        )
        combined = "\n".join((pre, post))
        if category == "success_f2p":
            detail = "valid_fail_to_pass"
            reason = f"Official F2P: {', '.join(report['tests_pred']['FAIL_TO_PASS'])}"
        elif category == "passes_both_bug_not_triggered":
            detail = "trigger_not_reproduced"
            reason = f"Generated test passed on buggy and fixed revisions: {', '.join(report['tests_pred']['PASS_TO_PASS'])}"
        elif category == "fails_both_wrong_oracle_or_shared_failure":
            detail = detail_class(combined)
            reason = f"Generated test failed on both revisions: {', '.join(report['tests_pred']['FAIL_TO_FAIL'])}; {evidence(combined)}"
        elif category == "wrong_direction_pass_to_fail":
            detail = detail_class(combined)
            reason = f"Generated test passed on buggy but failed on fixed: {', '.join(report['tests_pred']['PASS_TO_FAIL'])}; {evidence(post or pre)}"
        else:
            detail = detail_class(combined)
            reason = evidence(combined)
        if root_attribution == "evaluation_environment_invalid_reference":
            reference_output = "\n".join((gold_pre, gold_post))
            detail = detail_class(reference_output)
            reason = "Golden/reference execution is also invalid: " + evidence(reference_output)

        records.append({
            "instance_id": instance_id,
            "repo": instance_id.rsplit("-", 1)[0],
            "resolved": bool(report.get("resolved")),
            "evaluation_valid": evaluation_valid,
            "root_attribution": root_attribution,
            "primary_category": category,
            "detail_category": detail,
            "reason": reason,
            "added_f2p": report.get("added_f2p"),
            "coverage_pred": report.get("coverage_pred"),
            "coverage_delta_pred": report.get("coverage_delta_pred"),
            "tests_pred": {key: report["tests_pred"].get(key, []) for key in TRANSITIONS},
            "tests_gold": {key: report["tests_gold"].get(key, []) for key in TRANSITIONS},
            "gold_reference_parsed_tests": gold_parsed,
            "feedback_rounds": feedback.get(instance_id, {}),
            "report_path": str(report_path),
            "pred_pre_output": pre_path,
            "pred_post_output": post_path,
            "gold_pre_output": gold_pre_path,
            "gold_post_output": gold_post_path,
        })

    records.sort(key=lambda item: item["instance_id"])
    failures = [item for item in records if not item["resolved"]]
    primary = Counter(item["primary_category"] for item in records)
    root_attributions = Counter(item["root_attribution"] for item in records)
    detail = Counter(item["detail_category"] for item in failures)
    repos = defaultdict(lambda: Counter(total=0, success=0))
    for item in records:
        repos[item["repo"]]["total"] += 1
        repos[item["repo"]]["success"] += int(item["resolved"])

    output_dir = run / "evaluation" / "failure_attribution"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "per_instance.json").write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with (output_dir / "per_instance.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = ("instance_id", "repo", "resolved", "evaluation_valid", "root_attribution", "primary_category", "detail_category", "reason", "added_f2p", "coverage_pred", "coverage_delta_pred")
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in records:
            writer.writerow({key: item[key] for key in fields})

    repo_rows = []
    for repo, count in sorted(repos.items()):
        repo_rows.append((repo, count["total"], count["success"], count["total"] - count["success"], 100 * count["success"] / count["total"]))
    summary = {
        "total": len(records),
        "success": sum(item["resolved"] for item in records),
        "failure": len(failures),
        "primary_counts": dict(primary.most_common()),
        "root_attribution_counts": dict(root_attributions.most_common()),
        "failure_detail_counts": dict(detail.most_common()),
        "repo_stats": [dict(repo=r, total=t, success=s, failure=f, success_percent=round(p, 2)) for r, t, s, f, p in repo_rows],
        "unresolved_with_nonzero_coverage": sum((item["coverage_pred"] or 0) > 0 for item in failures),
        "unresolved_with_nonzero_coverage_delta": sum((item["coverage_delta_pred"] or 0) > 0 for item in failures),
        "repair_failed_ids": sorted(iid for iid, rounds in feedback.items() if any(x.get("action") == "repair_failed" for x in rounds.values())),
        "round_3_new_repaired_candidates_not_buggy_revalidated": sum(
            rounds.get("3", {}).get("action") == "repaired" for rounds in feedback.values()
        ),
        "round_3_status_describes_round_2_candidate": True,
        "feedback_used_full_core_checkpoint_loop": False,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# SWT-Lite 276 failure attribution", "",
        f"- Total: {summary['total']}", f"- F2P success: {summary['success']}", f"- Unresolved: {summary['failure']}", "",
        "## Primary attribution", "", "| Category | Count |", "|---|---:|",
    ]
    lines += [f"| {key} | {value} |" for key, value in primary.most_common()]
    lines += ["", "## Root attribution (gold/reference validity checked)", "", "| Attribution | Count |", "|---|---:|"]
    lines += [f"| {key} | {value} |" for key, value in root_attributions.most_common()]
    lines += ["", "## Failure detail", "", "| Detail | Count |", "|---|---:|"]
    lines += [f"| {key} | {value} |" for key, value in detail.most_common()]
    lines += ["", "## Repository results", "", "| Repository | Total | F2P | Failed | F2P % |", "|---|---:|---:|---:|---:|"]
    lines += [f"| {repo} | {total} | {success} | {failure} | {percent:.2f} |" for repo, total, success, failure, percent in repo_rows]
    lines += ["", "## Every unresolved instance", "", "| Instance | Root attribution | Primary | Detail | Coverage | Delta | Evidence |", "|---|---|---|---|---:|---:|---|"]
    for item in failures:
        reason = item["reason"].replace("|", "\\|").replace("\n", " ")[:240]
        lines.append(f"| {item['instance_id']} | {item['root_attribution']} | {item['primary_category']} | {item['detail_category']} | {item['coverage_pred']} | {item['coverage_delta_pred']} | {reason} |")
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
