"""Secret-safe multi-endpoint API pool configuration.

Real credentials must live in an ignored local file or environment variables,
never in this tracked module.  See ``config/api_pool.example.json``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v3"

# Compatibility names retained for callers that imported the old constants.
API_NAMES: list[str] = []
API_KEYS: list[str] = []
API_BASE_URLS: list[str] = []
API_MODELS: list[str] = []


def _split_env(name: str) -> list[str]:
    raw = str(os.environ.get(name) or "")
    return [item.strip() for item in re.split(r"[,\n]", raw) if item.strip()]


def _expand(values: list[str], size: int, default: str) -> list[str]:
    if not values:
        return [default] * size
    if len(values) == 1:
        return values * size
    if len(values) != size:
        raise ValueError(
            f"API pool field has {len(values)} values but {size} keys were configured"
        )
    return values


def _normalize_entries(payload: Any) -> list[tuple[str, str, str]]:
    rows = payload.get("apis", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("API pool JSON must be a list or an object containing 'apis'")
    entries: list[tuple[str, str, str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"API pool entry {index} must be an object")
        key = str(row.get("api_key") or row.get("key") or "").strip()
        if not key:
            continue
        base_url = str(row.get("base_url") or DEFAULT_BASE_URL).strip()
        model = str(row.get("model") or DEFAULT_MODEL).strip()
        entries.append((key, base_url, model))
    return entries


def _configured_file() -> Path | None:
    explicit = str(os.environ.get("BRT_API_POOL_FILE") or "").strip()
    candidates = [Path(explicit).expanduser()] if explicit else []
    project_root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            project_root / ".secrets" / "api_pool.json",
            Path.home() / ".config" / "brt5" / "api_pool.json",
        ]
    )
    return next((path for path in candidates if path.is_file()), None)


def _entries_from_file(path: Path) -> list[tuple[str, str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid API pool file: {path}") from exc
    return _normalize_entries(payload)


def _entries_from_environment() -> list[tuple[str, str, str]]:
    json_payload = str(os.environ.get("BRT_API_POOL_JSON") or "").strip()
    if json_payload:
        try:
            return _normalize_entries(json.loads(json_payload))
        except json.JSONDecodeError as exc:
            raise ValueError("BRT_API_POOL_JSON is not valid JSON") from exc
    keys = _split_env("DEEPSEEK_API_KEYS") or _split_env("DEEPSEEK_API_KEY")
    if not keys:
        return []
    bases = _expand(
        _split_env("DEEPSEEK_BASE_URLS")
        or _split_env("DEEPSEEK_BASE_URL"),
        len(keys),
        DEFAULT_BASE_URL,
    )
    models = _expand(
        _split_env("DEEPSEEK_MODELS") or _split_env("DEEPSEEK_MODEL"),
        len(keys),
        DEFAULT_MODEL,
    )
    return list(zip(keys, bases, models))


def _legacy_local_entries() -> list[tuple[str, str, str]]:
    """Read the ignored pre-migration pool on this machine only.

    This compatibility path is never present in a clean clone or export.
    """
    legacy = Path(__file__).resolve().parents[1] / ".secrets" / "api_pool_legacy.py"
    if not legacy.is_file():
        return []
    spec = importlib.util.spec_from_file_location("brt5_local_api_pool", legacy)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    loader = getattr(module, "configured_apis", None)
    return list(loader()) if callable(loader) else []


def configured_apis() -> list[tuple[str, str, str]]:
    """Return validated ``(key, base_url, model)`` entries without logging keys."""
    path = _configured_file()
    if path is not None:
        return _entries_from_file(path)
    entries = _entries_from_environment()
    if entries:
        return entries
    return _legacy_local_entries()
