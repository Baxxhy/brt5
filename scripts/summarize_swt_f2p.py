#!/usr/bin/env python3
"""Summarize F2P and strict resolution from official SWTBench reports."""

import argparse
import json
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--swt-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def percent(numerator, denominator):
    return round(100.0 * numerator / denominator, 4) if denominator else 0.0


def mean(values):
    return sum(values) / len(values) if values else 0.0


def main():
    args = parse_args()
    prediction_ids = []
    with args.predictions.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                prediction_ids.append(json.loads(line)["instance_id"])

    model_dir = args.model_name.replace("/", "__")
    reports_dir = args.swt_root / "run_instance_swt_logs" / args.run_id / model_dir
    rows = {}
    for report_path in reports_dir.glob("*/report.json"):
        with report_path.open(encoding="utf-8") as handle:
            report = json.load(handle)
        for instance_id, result in report.items():
            rows[instance_id] = result

    all_ids = list(dict.fromkeys(prediction_ids))
    f2p_ids = sorted(
        instance_id for instance_id, result in rows.items()
        if int(result.get("added_f2p") or 0) > 0
    )
    resolved_ids = sorted(
        instance_id for instance_id, result in rows.items()
        if bool(result.get("resolved"))
    )
    error_ids = sorted(set(all_ids) - set(rows))
    total = len(all_ids)
    evaluated = len(set(all_ids) & set(rows))
    added_f2p_tests = sum(
        int(rows[instance_id].get("added_f2p") or 0)
        for instance_id in all_ids if instance_id in rows
    )
    coverage_by_instance = {
        instance_id: float(rows[instance_id].get("coverage_pred") or 0.0)
        for instance_id in all_ids if instance_id in rows
    }
    coverage_delta_by_instance = {
        instance_id: float(rows[instance_id].get("coverage_delta_pred") or 0.0)
        for instance_id in all_ids if instance_id in rows
    }
    coverage_values = list(coverage_by_instance.values())
    coverage_delta_values = list(coverage_delta_by_instance.values())

    summary = {
        "run_id": args.run_id,
        "model_name": args.model_name,
        "metric_definition": {
            "f2p_success": "Official report added_f2p > 0.",
            "f2p_at_1_percent": "F2P-success instances / all prediction instances * 100.",
            "official_resolved": "Official SWTBench resolved: gains F2P without a new regression.",
            "patch_coverage": "Official SWTBench report coverage_pred, macro-averaged over evaluated predictions.",
            "patch_coverage_delta": "Official SWTBench report coverage_delta_pred, macro-averaged over evaluated predictions.",
        },
        "total_instances": total,
        "evaluated_instances": evaluated,
        "error_instances": len(error_ids),
        "f2p_success_instances": len(f2p_ids),
        "f2p_added_tests": added_f2p_tests,
        "f2p_at_1_percent": percent(len(f2p_ids), total),
        "f2p_among_evaluated_percent": percent(len(f2p_ids), evaluated),
        "official_resolved_instances": len(resolved_ids),
        "official_resolved_at_1_percent": percent(len(resolved_ids), total),
        "patch_coverage_evaluated_instances": len(coverage_values),
        "mean_patch_coverage": mean(coverage_values),
        "mean_patch_coverage_percent": percent(sum(coverage_values), len(coverage_values)),
        "mean_patch_coverage_delta": mean(coverage_delta_values),
        "mean_patch_coverage_delta_percent": percent(
            sum(coverage_delta_values), len(coverage_delta_values)
        ),
        "patch_coverage_nonzero_instances": sum(value > 0 for value in coverage_values),
        "patch_coverage_by_instance": coverage_by_instance,
        "patch_coverage_delta_by_instance": coverage_delta_by_instance,
        "f2p_success_ids": f2p_ids,
        "official_resolved_ids": resolved_ids,
        "error_ids": error_ids,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        key: summary[key] for key in (
            "total_instances", "evaluated_instances", "error_instances",
            "f2p_success_instances", "f2p_added_tests", "f2p_at_1_percent",
            "official_resolved_instances", "official_resolved_at_1_percent",
            "patch_coverage_evaluated_instances", "mean_patch_coverage",
            "mean_patch_coverage_delta",
        )
    }, indent=2))


if __name__ == "__main__":
    main()
