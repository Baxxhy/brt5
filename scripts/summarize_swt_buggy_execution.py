#!/usr/bin/env python3
"""Build structured buggy-side evidence without another verifier model call."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from repair_swt_from_buggy_feedback import execution_from_log


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances", type=Path, required=True)
    parser.add_argument("--generation-dir", type=Path, required=True)
    parser.add_argument("--feedback-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    issues = json.loads(args.instances.read_text(encoding="utf-8"))
    rows = []
    for issue in issues:
        iid = issue["instance_id"]
        test = args.generation_dir / iid / "final_test.py"
        log = args.feedback_root / iid / "test_output.txt"
        if not test.is_file() or not log.is_file():
            rows.append({"instance_id": iid, "action": "missing", "verified_candidate": False})
            continue
        execution = execution_from_log(iid, log.read_text(encoding="utf-8", errors="replace"))
        rows.append({
            "instance_id": iid,
            "action": "executed",
            "status": execution.status,
            "returncode": execution.returncode,
            "candidate_sha256": hashlib.sha256(test.read_bytes()).hexdigest(),
            "verified_candidate": True,
            "golden_or_fixed_used": False,
        })
    payload = {
        "total": len(rows),
        "verified": sum(row.get("verified_candidate", False) for row in rows),
        "results": rows,
        "golden_or_fixed_used": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "results"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
