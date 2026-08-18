#!/usr/bin/env python3
"""Validate that golden/reference canaries really collected and executed."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from revalidate_swt_checkpoints import atomic_json, classify_test_output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--required-ids", type=Path, required=True)
    parser.add_argument("--attempt-root", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    required = {
        line.strip()
        for line in args.required_ids.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    latest: dict[str, Path] = {}
    for root in args.attempt_root:
        if not root.is_dir():
            continue
        for instance_dir in root.iterdir():
            output = instance_dir / "test_output.txt"
            if instance_dir.name in required and output.is_file():
                latest[instance_dir.name] = instance_dir

    rows = []
    for instance_id in sorted(required):
        instance_dir = latest.get(instance_id)
        if instance_dir is None:
            rows.append({
                "instance_id": instance_id,
                "accepted": False,
                "reason": "missing test_output.txt",
            })
            continue
        spec = json.loads((instance_dir / "exec_spec.json").read_text(encoding="utf-8"))
        result = classify_test_output(instance_dir / "test_output.txt", spec["repo"])
        accepted = bool(
            result["patch_applied"]
            and result["collected_count"] > 0
            and result["executed_count"] > 0
            and result["category"] not in {
                "execution_missing", "patch_apply_error", "timeout", "zero_test",
            }
        )
        rows.append({
            "instance_id": instance_id,
            "project": (
                "pytest" if instance_id.startswith("pytest-dev__")
                else "astropy" if instance_id.startswith("astropy__")
                else "sphinx" if instance_id.startswith("sphinx-doc__")
                else "other"
            ),
            "accepted": accepted,
            "source": str(instance_dir.resolve()),
            **result,
        })

    project_counts = {}
    for project, expected in (("pytest", 11), ("astropy", 2), ("sphinx", 4)):
        project_rows = [row for row in rows if row.get("project") == project]
        passed = sum(bool(row.get("accepted")) for row in project_rows)
        project_counts[project] = {"passed": passed, "expected": expected}
    gate_passed = (
        len(rows) == 17
        and all(value["passed"] == value["expected"] for value in project_counts.values())
    )
    payload = {
        "gate_passed": gate_passed,
        "criterion": "patch applied AND collected_count>0 AND executed_count>0",
        "project_counts": project_counts,
        "rows": rows,
    }
    atomic_json(args.output, payload)
    print(json.dumps({"gate_passed": gate_passed, "project_counts": project_counts}))
    return 0 if gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
