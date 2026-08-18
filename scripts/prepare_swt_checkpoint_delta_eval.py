#!/usr/bin/env python3
"""Prepare the exact formal-evaluation delta after checkpoint selection."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def read_jsonl(path: Path) -> dict[str, dict]:
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["instance_id"]] = row
    return rows


def digest(row: dict) -> str:
    return hashlib.sha256(str(row.get("model_patch") or "").encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-predictions", type=Path, required=True)
    parser.add_argument("--selected-predictions", type=Path, required=True)
    parser.add_argument("--environment-fixed-ids", type=Path, required=True)
    parser.add_argument("--output-ids", type=Path, required=True)
    parser.add_argument("--output-comparison", type=Path, required=True)
    args = parser.parse_args()
    previous = read_jsonl(args.previous_predictions)
    selected = read_jsonl(args.selected_predictions)
    if set(previous) != set(selected) or len(selected) != 276:
        raise RuntimeError(
            f"prediction universe mismatch: previous={len(previous)} selected={len(selected)}"
        )
    changed = sorted(
        instance_id
        for instance_id in selected
        if digest(previous[instance_id]) != digest(selected[instance_id])
    )
    environment_fixed = sorted({
        line.strip()
        for line in args.environment_fixed_ids.read_text(encoding="utf-8").splitlines()
        if line.strip()
    })
    unknown = set(environment_fixed) - set(selected)
    if unknown:
        raise RuntimeError(f"unknown environment-fixed instance IDs: {sorted(unknown)}")
    evaluate = sorted(set(changed) | set(environment_fixed))
    args.output_ids.parent.mkdir(parents=True, exist_ok=True)
    args.output_ids.write_text("\n".join(evaluate) + "\n", encoding="utf-8")
    payload = {
        "instances": len(selected),
        "changed_model_patch_sha256": len(changed),
        "environment_fixed": len(environment_fixed),
        "overlap": len(set(changed) & set(environment_fixed)),
        "formal_delta": len(evaluate),
        "reuse_previous_formal_result": len(selected) - len(evaluate),
        "changed_ids": changed,
        "environment_fixed_ids": environment_fixed,
        "formal_delta_ids": evaluate,
    }
    args.output_comparison.parent.mkdir(parents=True, exist_ok=True)
    args.output_comparison.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: payload[key] for key in (
        "instances", "changed_model_patch_sha256", "environment_fixed",
        "overlap", "formal_delta", "reuse_previous_formal_result",
    )}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
