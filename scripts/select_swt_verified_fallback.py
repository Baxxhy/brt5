#!/usr/bin/env python3
"""Select the latest safe BRT checkpoint using buggy-side evidence only.

The selector is deliberately conservative: it keeps the newest candidate when
that candidate was semantically accepted or produced a usable execution.  It
falls back only when a newer candidate is unverified/non-executable and an
earlier candidate was accepted by the existing BRT verifier.  Golden patches,
fixed-side output, F2P labels, and coverage are never read here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil


HARD_FAILURES = {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT"}
BUGGY_PASS_STATUSES = {"PASS", "BUGGY_PASS"}


def candidate_rank(row: dict) -> tuple[int, int, int, int, int]:
    """Reproduce the BRT5 checkpoint ordering from buggy-side evidence.

    A test that passes on the buggy revision cannot be F2P, so it must never
    replace an earlier, genuinely failing checkpoint merely because it is
    newer.  Semantic acceptance is a ranking signal inside the executable
    buggy-failure tier, not permission to select a PASS or infrastructure
    failure.  Earlier checkpoints win exact ties, matching BRT5's fallback.
    """
    return (
        int(row.get("surrogate_pass", False) and row["buggy_failure"]),
        int(row["buggy_failure"]),
        int(row["accepted"]),
        int(row["verified"] and not row["hard_failure"]),
        -int(row["order"]),
    )


def keyed_path(value: str) -> tuple[str, Path]:
    label, separator, raw_path = value.partition("=")
    if not separator or not label or not raw_path:
        raise argparse.ArgumentTypeError("expected LABEL=PATH")
    return label, Path(raw_path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_results(path: Path) -> dict[str, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {row["instance_id"]: row for row in payload.get("results", [])}


def copy_candidate(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        target.unlink() if target.is_symlink() else shutil.rmtree(target)
    shutil.copytree(source.resolve(), target, symlinks=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances", type=Path, required=True)
    parser.add_argument("--instance-ids-file", type=Path, default=None)
    parser.add_argument(
        "--candidate-generation", action="append", type=keyed_path, required=True,
        help="Ordered LABEL=PATH candidate generations, oldest to newest.",
    )
    parser.add_argument(
        "--surrogate-summary", action="append", type=keyed_path, default=[],
        help="Optional LABEL=PATH issue-only surrogate execution summary.",
    )
    parser.add_argument(
        "--verification-summary", action="append", type=keyed_path, required=True,
        help="LABEL=PATH buggy-side verifier summary for a candidate generation.",
    )
    parser.add_argument("--output-generation", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    issues = json.loads(args.instances.read_text(encoding="utf-8"))
    instance_ids = [row["instance_id"] for row in issues]
    if args.instance_ids_file:
        wanted = {
            line.strip()
            for line in args.instance_ids_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        instance_ids = [instance_id for instance_id in instance_ids if instance_id in wanted]
    candidates = args.candidate_generation
    verification = {label: load_results(path) for label, path in args.verification_summary}
    surrogate = {label: load_results(path) for label, path in args.surrogate_summary}
    args.output_generation.mkdir(parents=True, exist_ok=True)

    selections = []
    for instance_id in instance_ids:
        rows = []
        evidence_by_sha: dict[str, list[dict]] = {}
        for order, (label, generation) in enumerate(candidates):
            source = generation / instance_id
            test = source / "final_test.py"
            if not test.is_file():
                continue
            digest = file_sha256(test)
            evidence = verification.get(label, {}).get(instance_id)
            if evidence:
                evidence_by_sha.setdefault(digest, []).append(evidence)
            rows.append({
                "order": order,
                "label": label,
                "source": source,
                "sha256": digest,
                "evidence": evidence,
            })
        if not rows:
            raise RuntimeError(f"no generated candidate for {instance_id}")

        for row in rows:
            merged = evidence_by_sha.get(row["sha256"], [])
            row["accepted"] = any(item.get("action") == "accepted" for item in merged)
            row["verified"] = bool(merged)
            statuses = [str(item.get("status") or "") for item in merged]
            row["hard_failure"] = bool(statuses) and all(
                status in HARD_FAILURES for status in statuses
            )
            row["buggy_pass"] = any(status in BUGGY_PASS_STATUSES for status in statuses)
            row["buggy_failure"] = any(
                status and status not in HARD_FAILURES | BUGGY_PASS_STATUSES
                for status in statuses
            )
            surrogate_row = surrogate.get(row["label"], {}).get(instance_id, {})
            row["surrogate_pass"] = bool(
                surrogate_row.get("surrogate_pass")
                and surrogate_row.get("candidate_sha256") == row["sha256"]
            )
            row["rank_key"] = candidate_rank(row)

        newest = rows[-1]
        selected = max(rows, key=candidate_rank)
        if selected["surrogate_pass"] and selected["buggy_failure"]:
            reason = "buggy-failing candidate passed an issue-only surrogate source patch"
        elif selected["buggy_failure"] and selected["accepted"]:
            reason = "best verified buggy failure accepted by BRT5 semantic verifier"
        elif selected["buggy_failure"]:
            reason = "best verified executable buggy failure"
        elif selected["buggy_pass"]:
            reason = "no buggy-failing checkpoint; retain verified buggy PASS for formal scoring"
        elif selected["verified"] and not selected["hard_failure"]:
            reason = "no buggy-failing checkpoint; retain verified runtime outcome"
        else:
            reason = "no executable checkpoint; retain earliest verified candidate for formal scoring"

        copy_candidate(selected["source"], args.output_generation / instance_id)
        selections.append({
            "instance_id": instance_id,
            "selected_label": selected["label"],
            "selected_sha256": selected["sha256"],
            "latest_label": newest["label"],
            "latest_sha256": newest["sha256"],
            "fallback_used": selected["sha256"] != newest["sha256"],
            "reason": reason,
            "rank_key": list(selected["rank_key"]),
            "candidate_count": len(rows),
            "surrogate_pass": selected["surrogate_pass"],
            "golden_or_fixed_used": False,
        })

    payload = {
        "instance_count": len(instance_ids),
        "selected_count": len(selections),
        "fallback_count": sum(row["fallback_used"] for row in selections),
        "selection_policy": "BRT5 buggy-failure rank with earliest-checkpoint fallback",
        "golden_or_fixed_used": False,
        "selections": selections,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in payload.items() if key != "selections"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
