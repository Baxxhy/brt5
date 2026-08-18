from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "select_swt_verified_fallback.py"


def write_candidate(root: Path, instance_id: str, code: str) -> None:
    target = root / instance_id
    target.mkdir(parents=True)
    (target / "final_test.py").write_text(code, encoding="utf-8")
    (target / "summary.json").write_text(
        json.dumps({"instance_id": instance_id}), encoding="utf-8"
    )


def write_summary(path: Path, rows: list[dict]) -> None:
    path.write_text(json.dumps({"results": rows}), encoding="utf-8")


def test_selects_accepted_and_non_executable_fallbacks(tmp_path: Path) -> None:
    instances = tmp_path / "instances.json"
    instances.write_text(
        json.dumps([{"instance_id": value} for value in ("accepted", "latest", "hard")]),
        encoding="utf-8",
    )
    round0, round1 = tmp_path / "round0", tmp_path / "round1"
    for root in (round0, round1):
        root.mkdir()
    write_candidate(round0, "accepted", "def test_old():\n    assert False\n")
    write_candidate(round1, "accepted", "def test_new():\n    raise RuntimeError\n")
    write_candidate(round0, "latest", "def test_old():\n    assert False\n")
    write_candidate(round1, "latest", "def test_new():\n    assert 1 == 2\n")
    write_candidate(round0, "hard", "def test_old():\n    assert 1 == 2\n")
    write_candidate(round1, "hard", "def broken(:\n")

    verify0, verify1 = tmp_path / "verify0.json", tmp_path / "verify1.json"
    write_summary(verify0, [
        {"instance_id": "accepted", "action": "accepted", "status": "FAIL"},
        {"instance_id": "latest", "action": "rejected", "status": "FAIL"},
        {"instance_id": "hard", "action": "rejected", "status": "FAIL"},
    ])
    write_summary(verify1, [
        {"instance_id": "accepted", "action": "rejected", "status": "FAIL"},
        {"instance_id": "latest", "action": "rejected", "status": "FAIL"},
        {"instance_id": "hard", "action": "rejected", "status": "SYNTAX_ERROR"},
    ])
    output, manifest = tmp_path / "selected", tmp_path / "manifest.json"
    subprocess.run([
        sys.executable, str(SCRIPT),
        "--instances", str(instances),
        "--candidate-generation", f"round0={round0}",
        "--candidate-generation", f"round1={round1}",
        "--verification-summary", f"round0={verify0}",
        "--verification-summary", f"round1={verify1}",
        "--output-generation", str(output),
        "--manifest", str(manifest),
    ], check=True)

    selected = {
        row["instance_id"]: row
        for row in json.loads(manifest.read_text(encoding="utf-8"))["selections"]
    }
    assert selected["accepted"]["selected_label"] == "round0"
    # Equal buggy-side evidence keeps the earlier verified checkpoint instead
    # of changing the final test without a demonstrated improvement.
    assert selected["latest"]["selected_label"] == "round0"
    assert selected["hard"]["selected_label"] == "round0"
    assert json.loads(manifest.read_text(encoding="utf-8"))["golden_or_fixed_used"] is False


def test_buggy_pass_never_replaces_an_earlier_buggy_failure(tmp_path: Path) -> None:
    instances = tmp_path / "instances.json"
    instances.write_text(json.dumps([{"instance_id": "regression"}]), encoding="utf-8")
    round0, round1 = tmp_path / "round0", tmp_path / "round1"
    round0.mkdir()
    round1.mkdir()
    write_candidate(round0, "regression", "def test_old():\n    assert False\n")
    write_candidate(round1, "regression", "def test_new():\n    assert True\n")
    verify0, verify1 = tmp_path / "verify0.json", tmp_path / "verify1.json"
    write_summary(verify0, [{
        "instance_id": "regression", "action": "rejected", "status": "ASSERTION_FAIL",
    }])
    write_summary(verify1, [{
        "instance_id": "regression", "action": "rejected", "status": "PASS",
    }])
    output, manifest = tmp_path / "selected", tmp_path / "manifest.json"
    subprocess.run([
        sys.executable, str(SCRIPT),
        "--instances", str(instances),
        "--candidate-generation", f"round0={round0}",
        "--candidate-generation", f"round1={round1}",
        "--verification-summary", f"round0={verify0}",
        "--verification-summary", f"round1={verify1}",
        "--output-generation", str(output),
        "--manifest", str(manifest),
    ], check=True)
    row = json.loads(manifest.read_text(encoding="utf-8"))["selections"][0]
    assert row["selected_label"] == "round0"
    assert row["fallback_used"] is True


def test_issue_only_surrogate_pass_outranks_semantic_acceptance(tmp_path: Path) -> None:
    instances = tmp_path / "instances.json"
    instances.write_text(json.dumps([{"instance_id": "portfolio"}]), encoding="utf-8")
    primary, direct = tmp_path / "primary", tmp_path / "direct"
    primary.mkdir()
    direct.mkdir()
    write_candidate(primary, "portfolio", "def test_primary():\n    assert False\n")
    write_candidate(direct, "portfolio", "def test_direct():\n    assert False\n")
    verify_primary, verify_direct = tmp_path / "vp.json", tmp_path / "vd.json"
    write_summary(verify_primary, [{"instance_id": "portfolio", "action": "accepted", "status": "ASSERTION_FAIL"}])
    write_summary(verify_direct, [{"instance_id": "portfolio", "action": "rejected", "status": "ASSERTION_FAIL"}])
    digest = __import__("hashlib").sha256(
        (direct / "portfolio" / "final_test.py").read_bytes()
    ).hexdigest()
    surrogate = tmp_path / "surrogate.json"
    write_summary(surrogate, [{
        "instance_id": "portfolio", "candidate_sha256": digest, "surrogate_pass": True,
    }])
    output, manifest = tmp_path / "selected", tmp_path / "manifest.json"
    subprocess.run([
        sys.executable, str(SCRIPT),
        "--instances", str(instances),
        "--candidate-generation", f"primary={primary}",
        "--candidate-generation", f"direct={direct}",
        "--verification-summary", f"primary={verify_primary}",
        "--verification-summary", f"direct={verify_direct}",
        "--surrogate-summary", f"direct={surrogate}",
        "--output-generation", str(output),
        "--manifest", str(manifest),
    ], check=True)
    row = json.loads(manifest.read_text(encoding="utf-8"))["selections"][0]
    assert row["selected_label"] == "direct"
    assert row["surrogate_pass"] is True
    assert row["golden_or_fixed_used"] is False
