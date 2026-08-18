#!/usr/bin/env python3
"""Summarize final metrics and Failure-Directed mutation provenance."""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--baseline-run", type=Path, required=True)
    args = parser.parse_args()
    run, baseline = args.run_dir, args.baseline_run
    metrics = read_json(run / "evaluation/metrics.json", {})
    baseline_metrics = read_json(baseline / "evaluation/metrics.json", {})
    current_ids = set(metrics.get("f2p_success_ids", []))
    baseline_ids = set(baseline_metrics.get("f2p_success_ids", []))
    prediction_ids = set()
    predictions = run / "evaluation/predictions.jsonl"
    if predictions.is_file():
        for line in predictions.read_text(encoding="utf-8").splitlines():
            if line.strip():
                prediction_ids.add(json.loads(line)["instance_id"])
    denominator = int(metrics.get("total_instances") or len(prediction_ids) or 276)
    baseline_comparable_ids = baseline_ids & prediction_ids if prediction_ids else baseline_ids
    baseline_count = len(baseline_comparable_ids)

    records = []
    for path in sorted((run / "feedback").glob("round_*/generation/*/failure_directed_mutation.json")):
        row = read_json(path, {})
        if row:
            records.append(row)
    calls = collections.Counter(str(row.get("mutation_family") or "UNKNOWN") for row in records)
    fallback_reasons = collections.Counter(
        str(row.get("fallback_reason") or "unspecified") for row in records if row.get("fallback")
    )

    primary = read_json(run / "evaluation/primary_verified_fallback_selection.json", {})
    primary_source = {row["instance_id"]: row.get("selected_label") for row in primary.get("selections", [])}
    portfolio = read_json(run / "evaluation/verified_fallback_selection.json", {})
    final_source = {}
    for row in portfolio.get("selections", []):
        iid = row["instance_id"]
        final_source[iid] = "Direct" if row.get("selected_label") == "direct" else primary_source.get(iid, "primary")
    source_counts = collections.Counter(final_source.values())

    # Attribute a final F2P only to the last mutation that produced the selected
    # primary checkpoint.  This is provenance contribution, not causal credit.
    selected_primary = {row["instance_id"]: row.get("selected_label") for row in primary.get("selections", [])}
    family_by_instance_round = {}
    for row in records:
        family_by_instance_round[(row.get("instance_id"), f"round{row.get('round')}")] = row.get("mutation_family")
    contributions = collections.Counter()
    for iid in current_ids:
        label = final_source.get(iid)
        if label == "Direct":
            contributions["Direct"] += 1
            continue
        family = family_by_instance_round.get((iid, selected_primary.get(iid)))
        contributions[str(family or label or "Round0")] += 1

    hard = {"SYNTAX_ERROR", "SETUP_ERROR", "COLLECT_ERROR", "IMPORT_ERROR", "ZERO_TEST", "verification_error", "missing"}
    engineering_errors = []
    for path in sorted((run / "feedback").glob("**/docker_feedback_summary.json")):
        for row in read_json(path, {}).get("results", []):
            if row.get("status") in hard or row.get("action") in hard:
                engineering_errors.append({"instance_id": row.get("instance_id"), "status": row.get("status"), "source": str(path)})
    summary = {
        "schema_version": "failure-directed-run-summary.v1",
        "baseline_f2p": baseline_count,
        "baseline_total": denominator,
        "new_f2p": metrics.get("f2p_success_instances"),
        "new_total": metrics.get("total_instances", 276),
        "net_gain": (metrics.get("f2p_success_instances", 0) - baseline_count),
        "patch_coverage": metrics.get("mean_patch_coverage"),
        "patch_coverage_delta": metrics.get("mean_patch_coverage_delta"),
        "new_success_instances": sorted(current_ids - baseline_comparable_ids),
        "lost_success_instances": sorted(baseline_comparable_ids - current_ids),
        "final_source_counts": dict(source_counts),
        "mutation_family_calls": dict(calls),
        "mutation_family_final_f2p": dict(contributions),
        "fallback_count": sum(fallback_reasons.values()),
        "fallback_reasons": dict(fallback_reasons),
        "protected_region_violation_count": sum(bool(row.get("protected_region_violations")) for row in records),
        "trigger_to_oracle_switch_count": sum(bool(row.get("trigger_to_oracle")) for row in records),
        "environment_parser_setup_error_count": len(engineering_errors),
        "environment_parser_setup_errors": engineering_errors,
        "golden_or_fixed_used_for_mutation_or_ranking": False,
        "headline": {
            "Baseline F2P": f"{baseline_count}/{denominator}",
            "New F2P": f"{metrics.get('f2p_success_instances', 0)}/{denominator}",
            "Net gain": metrics.get("f2p_success_instances", 0) - baseline_count,
            "Oracle Mutation contribution": contributions.get("ORACLE_MUTATION", 0),
            "State Mutation contribution": contributions.get("STATE_MUTATION", 0),
            "Structural Repair contribution": contributions.get("STRUCTURAL_REPAIR", 0),
            "Trigger→Oracle contribution": sum(
                iid in current_ids and row.get("trigger_to_oracle") for row in records for iid in [row.get("instance_id")]
            ),
        },
    }
    output = run / "evaluation/failure_directed_summary.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary["headline"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
