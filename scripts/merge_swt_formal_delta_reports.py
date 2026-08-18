#!/usr/bin/env python3
"""Merge changed formal reports with unchanged prior reports without leakage."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import shutil


def prediction_ids(path: Path) -> list[str]:
    return [
        json.loads(line)["instance_id"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_report(root: Path, instance_id: str) -> tuple[dict, Path] | None:
    path = root / instance_id / "report.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if set(payload) != {instance_id}:
        raise RuntimeError(f"malformed report keys in {path}: {sorted(payload)}")
    return payload[instance_id], path


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--delta-ids", type=Path, required=True)
    parser.add_argument("--previous-reports", type=Path, required=True)
    parser.add_argument("--delta-reports", type=Path, required=True)
    parser.add_argument("--output-reports", type=Path, required=True)
    parser.add_argument("--output-summary", type=Path, required=True)
    args = parser.parse_args()

    all_ids = prediction_ids(args.predictions)
    if len(all_ids) != 276 or len(set(all_ids)) != 276:
        raise RuntimeError(f"expected 276 unique predictions, got {len(all_ids)}")
    delta_ids = {
        line.strip()
        for line in args.delta_ids.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    if not delta_ids <= set(all_ids):
        raise RuntimeError("delta IDs are not a subset of predictions")

    rows = {}
    provenance = {}
    missing = []
    source_counts = Counter()
    for instance_id in all_ids:
        use_delta = instance_id in delta_ids
        source_root = args.delta_reports if use_delta else args.previous_reports
        loaded = read_report(source_root, instance_id)
        source = "delta" if use_delta else "previous"
        if loaded is None:
            missing.append(instance_id)
            provenance[instance_id] = "formal_error" if use_delta else "missing_previous"
            source_counts[provenance[instance_id]] += 1
            continue
        result, source_path = loaded
        destination = args.output_reports / instance_id / "report.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
        rows[instance_id] = result
        provenance[instance_id] = source
        source_counts[source] += 1

    coverage = [float(row["coverage_pred"]) for row in rows.values() if row.get("coverage_pred") is not None]
    coverage_delta = [
        float(row["coverage_delta_pred"])
        for row in rows.values()
        if row.get("coverage_delta_pred") is not None
    ]
    f2p_ids = sorted(instance_id for instance_id, row in rows.items() if int(row.get("added_f2p") or 0) > 0)
    resolved_ids = sorted(instance_id for instance_id, row in rows.items() if bool(row.get("resolved")))
    summary = {
        "total_instances": len(all_ids),
        "evaluated_instances": len(rows),
        "error_instances": len(missing),
        "error_ids": sorted(missing),
        "source_counts": dict(source_counts),
        "f2p_success_instances": len(f2p_ids),
        "f2p_added_tests": sum(int(row.get("added_f2p") or 0) for row in rows.values()),
        "f2p_at_1_percent": round(100 * len(f2p_ids) / len(all_ids), 4),
        "official_resolved_instances": len(resolved_ids),
        "official_resolved_at_1_percent": round(100 * len(resolved_ids) / len(all_ids), 4),
        "patch_coverage_evaluated_instances": len(coverage),
        "mean_patch_coverage": mean(coverage),
        "mean_patch_coverage_delta": mean(coverage_delta),
        "f2p_success_ids": f2p_ids,
        "official_resolved_ids": resolved_ids,
        "provenance": provenance,
    }
    args.output_summary.parent.mkdir(parents=True, exist_ok=True)
    args.output_summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: summary[key] for key in (
        "total_instances", "evaluated_instances", "error_instances",
        "source_counts", "f2p_success_instances", "f2p_at_1_percent",
        "official_resolved_instances", "official_resolved_at_1_percent",
        "patch_coverage_evaluated_instances", "mean_patch_coverage",
        "mean_patch_coverage_delta",
    )}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
