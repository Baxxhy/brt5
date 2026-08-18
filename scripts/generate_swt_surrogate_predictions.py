#!/usr/bin/env python3
"""Generate issue-only source patches and combine them with BRT candidates.

The resulting predictions are used only as a candidate-selection probe on the
buggy revision.  Golden patches, fixed source, and formal F2P labels are never
loaded by this script.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from brt6.core.schema import CandidateTest, ExecutionResult, RetrievedCode
from brt6.execution.patch_utils import (
    _apply_patch_items,
    _effective_surrogate_sources,
    _validate_patch_items,
    generate_surrogate_patch,
)
from brt6.io.io_utils import infer_repo_path, load_issue_data
from brt6.issue.issue_rewriter import (
    apply_behavior_safety_constraints,
    apply_issue_authority_constraints,
    behavior_from_dict,
)
from brt6.llm.llm_client import LLMClient

from export_swt_predictions import _new_file_patch, _safe_relative_path
from repair_swt_from_buggy_feedback import execution_from_log, load_code


HARD_OR_PASS = {"PASS", "BUGGY_PASS", "SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT"}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_verification(path: Path) -> dict[str, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {row["instance_id"]: row for row in payload.get("results", [])}


def construct_candidate(instance_id: str, source: Path) -> tuple[CandidateTest, dict]:
    summary = json.loads((source / "summary.json").read_text(encoding="utf-8"))
    code = (source / "final_test.py").read_text(encoding="utf-8")
    return CandidateTest(
        instance_id=instance_id,
        code=code,
        candidate_repo_path=_safe_relative_path(summary, instance_id),
        command=str(summary.get("command") or ""),
    ), summary


def materialize_sources(
    repo: Path,
    base_commit: str,
    related_code: list[RetrievedCode],
    root: Path,
) -> list[RetrievedCode]:
    materialized: list[RetrievedCode] = []
    seen: set[tuple[str, str]] = set()
    source_by_path: dict[str, str] = {}
    for item in related_code:
        path = str(item.path or "").strip().replace("\\", "/")
        key = (path, str(item.obj_name or ""))
        if not path or key in seen or path.startswith("/") or ".." in Path(path).parts:
            continue
        if path not in source_by_path:
            proc = subprocess.run(
                ["git", "-C", str(repo), "show", f"{base_commit}:{path}"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if proc.returncode != 0:
                continue
            source_by_path[path] = proc.stdout
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(proc.stdout, encoding="utf-8")
        payload = {field.name: getattr(item, field.name) for field in fields(RetrievedCode)}
        payload["code_content"] = source_by_path[path]
        materialized.append(RetrievedCode(**payload))
        seen.add(key)
    return materialized


def process_one(
    instance_id: str,
    issue: dict,
    related_code: list[RetrievedCode],
    verification: dict,
    args: argparse.Namespace,
) -> dict:
    source = args.generation_dir / instance_id
    status = str(verification.get("status") or "")
    if status in HARD_OR_PASS or not status:
        return {"instance_id": instance_id, "status": "SKIPPED_NOT_BUGGY_FAILURE", "buggy_status": status}
    log_path = args.feedback_root / instance_id / "test_output.txt"
    if not log_path.is_file():
        return {"instance_id": instance_id, "status": "SKIPPED_MISSING_BUGGY_LOG", "buggy_status": status}

    candidate, summary = construct_candidate(instance_id, source)
    issue_text = str(issue.get("issue_text") or issue.get("problem_statement") or "")
    behavior = behavior_from_dict(
        instance_id,
        json.loads((source / "behavior_target.json").read_text(encoding="utf-8")),
    )
    behavior = apply_issue_authority_constraints(issue_text, behavior)
    behavior = apply_behavior_safety_constraints(issue_text, behavior)
    buggy_log = log_path.read_text(encoding="utf-8", errors="replace")
    execution = execution_from_log(instance_id, buggy_log)
    repo = Path(infer_repo_path(str(args.repo_root), issue, instance_id))

    output_dir = args.output_dir / instance_id
    output_dir.mkdir(parents=True, exist_ok=True)
    client = LLMClient(
        provider="deepseek",
        model=args.model,
        temperature=0.1,
        max_tokens=args.max_tokens,
    )
    attempts: list[dict] = []
    with tempfile.TemporaryDirectory(prefix=f"brt6_surrogate_{instance_id}_", dir=os.environ.get("TMPDIR")) as raw_tmp:
        temp_root = Path(raw_tmp)
        effective = _effective_surrogate_sources(behavior, related_code, str(repo))
        materialized = materialize_sources(
            repo, str(issue.get("base_commit") or "HEAD"), effective, temp_root
        )
        if not materialized:
            return {"instance_id": instance_id, "status": "NO_SOURCE_CONTEXT", "buggy_status": status}
        for round_id in range(args.max_rounds):
            try:
                patch_candidate = generate_surrogate_patch(
                    instance_id, behavior, candidate, materialized, execution,
                    client, str(output_dir), str(temp_root), round_id, attempts,
                )
                patch_items, error = _validate_patch_items(
                    patch_candidate, materialized, candidate.candidate_repo_path
                )
                if error:
                    attempts.append({"round_id": round_id, "status": "REJECTED", "reason": error})
                    continue
                applied_paths, diff, error = _apply_patch_items(str(temp_root), patch_items)
                if error:
                    attempts.append({"round_id": round_id, "status": "APPLY_ERROR", "reason": error})
                    continue
                test_patch = _new_file_patch(candidate.candidate_repo_path, candidate.code)
                combined = test_patch + ("\n" if test_patch and diff else "") + diff
                record = {
                    "instance_id": instance_id,
                    "status": "GENERATED",
                    "buggy_status": status,
                    "candidate_sha256": sha256_text(candidate.code),
                    "candidate_repo_path": candidate.candidate_repo_path,
                    "source_paths": applied_paths,
                    "source_diff": diff,
                    "model_patch": combined,
                    "golden_or_fixed_used": False,
                }
                (output_dir / "surrogate_record.json").write_text(
                    json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                return record
            except Exception as exc:  # one candidate must not abort the portfolio
                attempts.append({"round_id": round_id, "status": "ERROR", "reason": str(exc)})
    return {
        "instance_id": instance_id,
        "status": "NO_APPLICABLE_PATCH",
        "buggy_status": status,
        "candidate_sha256": sha256_text(candidate.code),
        "attempts": attempts,
        "golden_or_fixed_used": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances", type=Path, required=True)
    parser.add_argument("--code-retrieval", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--generation-dir", type=Path, required=True)
    parser.add_argument("--verification-summary", type=Path, required=True)
    parser.add_argument("--feedback-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--model", default="DeepSeek-V4-Flash")
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--max-rounds", type=int, default=2)
    args = parser.parse_args()

    issues = load_issue_data(str(args.instances))
    code = load_code(args.code_retrieval)
    verified = load_verification(args.verification_summary)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {
            pool.submit(process_one, iid, issue, code.get(iid, []), verified.get(iid, {}), args): iid
            for iid, issue in issues.items()
        }
        for future in as_completed(futures):
            iid = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"instance_id": iid, "status": "ERROR", "error": str(exc)})
    results.sort(key=lambda row: row["instance_id"])
    eligible = [row for row in results if row.get("status") == "GENERATED"]
    args.predictions.parent.mkdir(parents=True, exist_ok=True)
    args.predictions.write_text(
        "\n".join(json.dumps({
            "instance_id": row["instance_id"],
            "model_name_or_path": "brt6__issue_only_surrogate",
            "model_patch": row["model_patch"],
        }, ensure_ascii=False) for row in eligible) + ("\n" if eligible else ""),
        encoding="utf-8",
    )
    for row in results:
        row.pop("model_patch", None)
        row.pop("source_diff", None)
    (args.output_dir / "surrogate_generation_summary.json").write_text(
        json.dumps({"total": len(results), "generated": len(eligible), "results": results,
                    "golden_or_fixed_used": False}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"total": len(results), "generated": len(eligible)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
