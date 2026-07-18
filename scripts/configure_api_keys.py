#!/usr/bin/env python3
"""Create or validate the ignored local multi-key API configuration."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / ".secrets" / "api_pool.json"


def _split_keys(value: str) -> list[str]:
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def _entries_from_environment() -> list[dict[str, str]]:
    raw = os.environ.get("DEEPSEEK_API_KEYS") or os.environ.get("DEEPSEEK_API_KEY") or ""
    keys = _split_keys(raw)
    base_url = os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    model = os.environ.get("DEEPSEEK_MODEL") or "deepseek-v3"
    return [
        {
            "name": f"deepseek-{index}",
            "api_key": key,
            "base_url": base_url,
            "model": model,
        }
        for index, key in enumerate(keys, 1)
    ]


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    path.chmod(0o600)


def _validate(path: Path) -> int:
    if not path.is_file():
        print(f"API pool is not configured: {path}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Invalid API pool file: {exc}", file=sys.stderr)
        return 1
    rows = payload.get("apis", []) if isinstance(payload, dict) else []
    valid = [row for row in rows if isinstance(row, dict) and str(row.get("api_key") or "").strip()]
    if not valid:
        print("API pool contains no usable keys", file=sys.stderr)
        return 1
    mode = oct(path.stat().st_mode & 0o777)
    print(f"API pool ready: entries={len(valid)}, permissions={mode}, path={path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--from-env", action="store_true")
    args = parser.parse_args()
    output = Path(args.output).expanduser().resolve()
    if args.check:
        return _validate(output)

    entries = _entries_from_environment() if args.from_env else []
    if not entries:
        raw_keys = getpass.getpass(
            "Paste one key or comma-separated keys (input hidden): "
        )
        keys = _split_keys(raw_keys)
        if not keys:
            print("No keys supplied", file=sys.stderr)
            return 2
        base_url = input("Base URL [https://api.deepseek.com]: ").strip() or "https://api.deepseek.com"
        model = input("Model [deepseek-v3]: ").strip() or "deepseek-v3"
        entries = [
            {
                "name": f"deepseek-{index}",
                "api_key": key,
                "base_url": base_url,
                "model": model,
            }
            for index, key in enumerate(keys, 1)
        ]
    _write_private_json(output, {"apis": entries})
    return _validate(output)


if __name__ == "__main__":
    raise SystemExit(main())
