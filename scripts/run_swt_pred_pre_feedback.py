#!/usr/bin/env python3
"""Run only generated tests on buggy commits for iterative feedback.

This deliberately never applies or reads the golden code/test patches during
candidate generation.  Final scoring remains the pinned official six-phase
SWTBench evaluation.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

from src.auxillary_src.extract_patches import remove_binary_diffs
from src.dataset import get_dataset_from_preds
from src.run_evaluation import run_eval_exec_spec
from src.test_spec import make_test_spec
from src.utils import get_test_directives

from run_swt_official_env_compat import (
    apply_environment_compatibility,
    install_retryable_repo_setup,
    install_retryable_source_fetches,
    install_quiet_eval_diagnostics,
    install_tolerant_docker_output,
)


def load_predictions(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return {row["instance_id"]: row for row in rows}


def run_one(instance: dict, prediction: dict, args: argparse.Namespace) -> str:
    test_spec = make_test_spec(instance)
    spec = test_spec.exec_spec
    patch = remove_binary_diffs(prediction["model_patch"])
    spec.timeout = args.timeout
    spec.rm_image = False
    spec.force_rebuild = False
    spec.run_id = args.run_id
    spec.compute_coverage = False
    spec.patch_id = args.patch_id
    spec.test_directives = get_test_directives(patch, spec.repo)
    spec.patch_list = [patch]
    run_eval_exec_spec(spec, patch, build_mode="api")
    return spec.instance_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--patch-id", required=True)
    parser.add_argument("--max-workers", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=3000)
    args = parser.parse_args()

    install_retryable_source_fetches()
    apply_environment_compatibility()
    install_retryable_repo_setup()
    install_quiet_eval_diagnostics(skip_reinstall=True)
    install_tolerant_docker_output()
    predictions = load_predictions(args.predictions)
    instances = get_dataset_from_preds(
        "princeton-nlp/SWE-bench_Lite",
        "test",
        list(predictions),
        predictions,
        args.run_id,
        exclude_completed=False,
        filter_swt=True,
    )
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {
            pool.submit(run_one, row, predictions[row["instance_id"]], args): row["instance_id"]
            for row in instances
        }
        for future in as_completed(futures):
            instance_id = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                failures[instance_id] = str(exc)
    Path("feedback_execution_summary.json").write_text(
        json.dumps(
            {
                "total": len(instances),
                "completed": len(instances) - len(failures),
                "failures": failures,
                "golden_patches_used": False,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # Infrastructure failures are per-candidate evidence.  The repair stage
    # carries those candidates forward and the official evaluator ultimately
    # counts unresolved items as failures.  Do not abort the other 275 items.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
