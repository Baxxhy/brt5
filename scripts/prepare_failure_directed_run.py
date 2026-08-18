#!/usr/bin/env python3
"""Freeze the reused baseline inputs for a Failure-Directed V1 run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--expected-instances", type=int, default=276)
    args = parser.parse_args()
    source = args.source_run.resolve()
    run = args.run_dir.resolve()
    if source == run:
        raise SystemExit("source and derived run directories must differ")
    generations = {"Round0": source / "generation", "Direct": source / "generation_direct"}
    records = {}
    for label, root in generations.items():
        if not root.is_dir():
            raise SystemExit(f"missing reused generation: {root}")
        instances = {}
        for child in sorted(root.iterdir()):
            test = child / "final_test.py"
            if not test.is_file():
                continue
            artifacts = {"final_test.py": sha256(test)}
            for name in ("behavior_target.json", "host_context.json", "summary.json", "mutation_round_0_plan.json"):
                path = child / name
                if path.is_file():
                    artifacts[name] = sha256(path)
            instances[child.name] = artifacts
        if len(instances) != args.expected_instances:
            raise SystemExit(f"{label} has {len(instances)} candidates, expected {args.expected_instances}")
        records[label] = {"path": str(root), "instance_count": len(instances), "artifacts": instances}
    run.mkdir(parents=True, exist_ok=True)
    reused = run / "reused"
    reused.mkdir(exist_ok=True)
    for label, root in generations.items():
        link = reused / label.lower()
        if not link.exists():
            link.symlink_to(root, target_is_directory=True)
    payload = {
        "schema_version": "failure-directed-reuse.v1",
        "source_run": str(source),
        "derived_run": str(run),
        "reused": records,
        "regenerated": [],
        "controlled_variable": "free-form iterative feedback -> Failure-Directed Mutation Controller V1",
        "golden_or_fixed_used": False,
    }
    project = Path(__file__).resolve().parents[1]
    method_files = [
        "mutation/failure_directed.py",
        "generation/generator.py",
        "scripts/repair_swt_from_buggy_feedback.py",
        "scripts/run_swt_iterative_docker_full.sh",
        "scripts/select_swt_verified_fallback.py",
        "scripts/summarize_failure_directed_run.py",
    ]
    snapshot = run / "method_snapshot"
    snapshot.mkdir(exist_ok=True)
    payload["controller_files"] = {}
    for relative in method_files:
        source_file = project / relative
        target_file = snapshot / relative
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)
        payload["controller_files"][relative] = {
            "sha256": sha256(source_file), "snapshot": str(target_file)
        }
    (run / "reuse_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"source_run": str(source), "round0": 276, "direct": 276}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
