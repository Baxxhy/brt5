#!/usr/bin/env python3
"""Classify issue-only surrogate executions for candidate ranking."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from repair_swt_from_buggy_feedback import execution_from_log


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generation-summary", type=Path, required=True)
    parser.add_argument("--feedback-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    generated = json.loads(args.generation_summary.read_text(encoding="utf-8"))
    rows = []
    for item in generated.get("results", []):
        if item.get("status") != "GENERATED":
            continue
        iid = item["instance_id"]
        log = args.feedback_root / iid / "test_output.txt"
        if not log.is_file():
            rows.append({**item, "surrogate_status": "MISSING", "surrogate_pass": False})
            continue
        execution = execution_from_log(iid, log.read_text(encoding="utf-8", errors="replace"))
        rows.append({
            "instance_id": iid,
            "candidate_sha256": item.get("candidate_sha256", ""),
            "buggy_status": item.get("buggy_status", ""),
            "surrogate_status": execution.status,
            "surrogate_pass": execution.status == "PASS",
            "golden_or_fixed_used": False,
        })
    payload = {
        "total": len(rows),
        "surrogate_pass": sum(row["surrogate_pass"] for row in rows),
        "results": rows,
        "golden_or_fixed_used": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "results"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
