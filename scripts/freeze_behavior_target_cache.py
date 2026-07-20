#!/usr/bin/env python3
"""Freeze one completed IssueRewrite output as a portable cache."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from brt5.core.behavior_target_cache import freeze_behavior_target_cache  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--instances-path", required=True)
    parser.add_argument("--cache-id", required=True)
    parser.add_argument("--dataset-mode", choices=("swt", "tdd"), required=True)
    parser.add_argument("--code-retrieval-path", required=True)
    parser.add_argument("--test-retrieval-path", required=True)
    parser.add_argument("--source-run-id", default="")
    parser.add_argument("--source-model", default="")
    parser.add_argument("--source-temperature", type=float, default=None)
    args = parser.parse_args()
    metadata = {
        key: value
        for key, value in {
            "source_run_id": args.source_run_id,
            "source_model": args.source_model,
            "source_temperature": args.source_temperature,
        }.items()
        if value not in {"", None}
    }
    try:
        provenance = freeze_behavior_target_cache(
            args.source_dir,
            args.output_dir,
            args.instances_path,
            cache_id=args.cache_id,
            dataset_mode=args.dataset_mode,
            code_retrieval_path=args.code_retrieval_path,
            test_retrieval_path=args.test_retrieval_path,
            source_metadata=metadata,
        )
    except ValueError as exc:
        print(f"BehaviorTarget cache freeze failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(provenance, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
