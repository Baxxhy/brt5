#!/usr/bin/env python3
"""Serially create the canonical SWT dependency-template environments.

This intentionally reuses the same iCoRe execution specifications and
``ensure_icore_environment`` implementation as generation.  It does not
install benchmark projects into the templates; generation creates an isolated
clone and installs the corresponding checkout there.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from brt5.retrieval.icore_runtime import (  # noqa: E402
    ensure_icore_environment,
    env_name_for,
    make_instance_spec,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prebuild SWT dependency-template Conda environments serially."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=PROJECT_ROOT / "data/issues/swt276_issues.json",
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        default=PROJECT_ROOT / ".bootstrap/swt-template-environments",
    )
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Print the canonical environments without creating them.",
    )
    return parser.parse_args()


def load_unique_templates(dataset_path: Path) -> list[dict[str, str]]:
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"dataset must contain a JSON list: {dataset_path}")

    templates: dict[str, dict[str, str]] = {}
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        repo = str(raw.get("repo") or "")
        version = str(raw.get("version") or "")
        base_commit = str(raw.get("base_commit") or "")
        environment_setup_commit = str(
            raw.get("environment_setup_commit") or base_commit
        )
        instance_id = str(raw.get("instance_id") or "")
        if not all((repo, version, base_commit, environment_setup_commit, instance_id)):
            raise ValueError(f"incomplete environment metadata: {raw!r}")
        env_name = env_name_for(
            repo,
            version,
            base_commit,
            environment_setup_commit,
        )
        templates.setdefault(
            env_name,
            {
                "env_name": env_name,
                "instance_id": instance_id,
                "repo": repo,
                "version": version,
                "base_commit": base_commit,
                "environment_setup_commit": environment_setup_commit,
            },
        )
    return sorted(
        templates.values(),
        key=lambda item: (item["repo"], item["version"], item["env_name"]),
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def prepare_template(
    template: dict[str, str],
    work_root: Path,
    timeout: int,
    retries: int,
) -> dict[str, Any]:
    env_name = template["env_name"]
    workdir = work_root / env_name
    workdir.mkdir(parents=True, exist_ok=True)
    attempts: list[dict[str, Any]] = []

    for attempt_number in range(1, retries + 1):
        started = time.time()
        try:
            spec = make_instance_spec(
                template["instance_id"],
                template["repo"],
                template["version"],
                template["base_commit"],
                template["environment_setup_commit"],
            )
            result = ensure_icore_environment(
                spec,
                env_name,
                str(workdir),
                timeout,
            )
        except Exception as exc:  # Preserve the complete failure for resume/audit.
            result = {
                "status": "EXCEPTION",
                "returncode": 1,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            }
        attempt = {
            "attempt": attempt_number,
            "elapsed_seconds": round(time.time() - started, 3),
            "result": result,
        }
        attempts.append(attempt)
        write_json(workdir / f"attempt_{attempt_number}.json", attempt)
        if int(result.get("returncode", 1)) == 0:
            return {
                **template,
                "status": "READY",
                "attempts": attempts,
                "result": result,
            }
        if attempt_number < retries:
            time.sleep(min(5 * attempt_number, 15))

    return {
        **template,
        "status": "FAILED",
        "attempts": attempts,
        "result": attempts[-1]["result"] if attempts else {},
    }


def main() -> int:
    args = parse_args()
    if args.timeout <= 0 or args.retries <= 0:
        raise ValueError("--timeout and --retries must be positive")
    dataset = args.dataset.expanduser().resolve()
    work_root = args.work_root.expanduser().resolve()
    templates = load_unique_templates(dataset)

    print(f"dataset={dataset}", flush=True)
    print(f"unique_template_environments={len(templates)}", flush=True)
    if args.list_only:
        for index, template in enumerate(templates, 1):
            print(
                f"[{index:02d}/{len(templates):02d}] "
                f"{template['env_name']} ({template['repo']} {template['version']})"
            )
        return 0

    work_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for index, template in enumerate(templates, 1):
        print(
            f"[{index:02d}/{len(templates):02d}] PREPARE {template['env_name']}",
            flush=True,
        )
        result = prepare_template(
            template,
            work_root,
            args.timeout,
            args.retries,
        )
        results.append(result)
        write_json(work_root / "prewarm_results.json", results)
        print(
            f"[{index:02d}/{len(templates):02d}] {result['status']} "
            f"{template['env_name']}",
            flush=True,
        )

    ready = [item["env_name"] for item in results if item["status"] == "READY"]
    failed = [item["env_name"] for item in results if item["status"] != "READY"]
    summary = {
        "dataset": str(dataset),
        "work_root": str(work_root),
        "total": len(results),
        "ready_count": len(ready),
        "failed_count": len(failed),
        "ready": ready,
        "failed": failed,
    }
    write_json(work_root / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
