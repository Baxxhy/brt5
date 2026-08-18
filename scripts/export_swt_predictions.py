#!/usr/bin/env python3
"""Export generated BRT files as SWT-Bench test-patch predictions."""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path, PurePosixPath


def _rows(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else list(payload.values())
    return [row for row in rows if isinstance(row, dict)]


def _safe_relative_path(summary: dict, instance_id: str) -> str:
    raw = str(
        summary.get("candidate_repo_path")
        or summary.get("direct_test_repo_path_hint")
        or ""
    ).strip()
    path = PurePosixPath(raw)
    if raw and not path.is_absolute() and ".." not in path.parts:
        return str(path)
    safe_id = "".join(ch if ch.isalnum() else "_" for ch in instance_id)
    return f"tests/test_brt_{safe_id}.py"


def _new_file_patch(relative_path: str, content: str) -> str:
    lines = content.splitlines(keepends=True)
    if content and not content.endswith("\n"):
        lines[-1] += "\n"
    body = list(
        difflib.unified_diff(
            [],
            lines,
            fromfile="/dev/null",
            tofile=f"b/{relative_path}",
            lineterm="\n",
        )
    )
    return (
        f"diff --git a/{relative_path} b/{relative_path}\n"
        "new file mode 100644\n"
        + "".join(body)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances", required=True, type=Path)
    parser.add_argument("--generation-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model-name", default="brt6__DeepSeek-V4-Flash")
    args = parser.parse_args()

    predictions: list[dict] = []
    generated = 0
    for row in _rows(args.instances):
        instance_id = str(row.get("instance_id") or "")
        instance_dir = args.generation_dir / instance_id
        test_path = instance_dir / "final_test.py"
        summary_path = instance_dir / "summary.json"
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = {}
        model_patch = ""
        if test_path.is_file():
            relative_path = _safe_relative_path(summary, instance_id)
            model_patch = _new_file_patch(
                relative_path,
                test_path.read_text(encoding="utf-8", errors="replace"),
            )
            generated += 1
        predictions.append(
            {
                "instance_id": instance_id,
                "model_name_or_path": args.model_name,
                "model_patch": model_patch,
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in predictions)
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"total": len(predictions), "generated": generated}))


if __name__ == "__main__":
    main()
