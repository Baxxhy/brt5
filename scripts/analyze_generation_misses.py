#!/usr/bin/env python3
"""Offline, trace-grounded analysis of Golden Oracle generation misses.

This script is deliberately analysis-only.  It reads already generated artifacts and
already cached buggy/fixed executions; it does not import the generation pipeline,
invoke a model, apply a golden patch, or execute a test.
"""

from __future__ import annotations

import argparse
import ast
import collections
import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


RUN_NAME = "brt6_swt276_deepseek_v4_flash_ccfa_portfolio_full_20260817_030000"
SOURCE_ORDER = {"Seed": -1, "Round0": 0, "Round1": 1, "Round2": 2, "Round3": 3,
                "Seed1": 11, "Seed2": 12, "Direct": 20}

TRIGGER_SUBTYPES = {
    "wrong_input_value_boundary": "输入值、形状或边界条件与 Issue 要求不符",
    "missing_trigger_condition": "缺少触发 bug 所必需的前置条件",
    "wrong_program_state": "调用发生时程序状态不正确",
    "wrong_fixture_setup_state": "fixture、模型、数据或配置状态不正确",
    "missing_api_call": "没有调用目标 API",
    "wrong_api_call": "调用了相邻但错误的 API/观察层",
    "wrong_call_sequence": "调用顺序、生命周期或刷新时机错误",
    "wrong_mock": "mock/patch 目标、返回值或调用方式错误",
    "target_path_not_reached": "测试执行了但没有进入目标实现路径",
    "other_trigger": "其他 Trigger 失败",
}
ORACLE_SUBTYPES = {
    "wrong_observed_variable_object": "观察了错误的变量、对象或层级",
    "wrong_expected_value": "期望值与修复后的真实语义不一致",
    "wrong_assertion_operator": "断言关系或比较算子错误",
    "wrong_exception_expectation": "异常类型、是否抛出或异常阶段预期错误",
    "assertion_too_strong": "断言过强，绑定了非必要细节",
    "assertion_too_weak": "断言过弱，buggy 和 fixed 均可通过",
    "secondary_symptom_assertion": "断言的是二级症状而非目标行为",
    "other_oracle": "其他 Oracle 失败",
}


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return default


def read_text(path: str | Path | None, limit: int = 1_000_000) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(errors="replace")[:limit]
    except OSError:
        return ""


def flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(flatten_text(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(flatten_text(v) for v in value)
    return "" if value is None else str(value)


def compact(text: str, width: int = 600) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:width] + ("..." if len(text) > width else "")


def code_excerpt(code: str, width: int = 1500) -> str:
    lines = [ln.rstrip() for ln in code.splitlines()]
    interesting = [i for i, ln in enumerate(lines) if re.search(
        r"\b(assert|assertRaises|raises|expect|mock|patch|monkeypatch)\b|\btest_", ln, re.I)]
    if not lines:
        return ""
    lo = max(0, (interesting[0] if interesting else 0) - 3)
    hi = min(len(lines), (interesting[-1] if interesting else min(len(lines), 24)) + 4)
    out = "\n".join(lines[lo:hi])
    return out[:width] + ("\n..." if len(out) > width else "")


def trace_excerpt(text: str, width: int = 1600) -> str:
    lines = text.splitlines()
    keep = []
    pattern = re.compile(
        r"FAILED|ERROR|Traceback|AssertionError|assert |E\s+|ImportError|TypeError|ValueError|"
        r"AttributeError|KeyError|RuntimeError|SyntaxError|collected|passed|failed|timeout", re.I)
    for i, line in enumerate(lines):
        # Runner cleanup and coverage dumps are not behavioral evidence.  In particular,
        # the single-line coverage JSON can be hundreds of KB and used to displace the
        # actual assertion from an excerpt.
        if len(line) > 900 or line.startswith(('+ git ', '+ cat coverage', 'HEAD is now', 'M\t')):
            continue
        if pattern.search(line):
            for nearby in lines[max(0, i - 2): min(len(lines), i + 3)]:
                if len(nearby) <= 900 and not nearby.startswith(('+ git ', '+ cat coverage', 'HEAD is now', 'M\t')):
                    keep.append(nearby)
    if not keep:
        keep = lines[-18:]
    # Stable de-duplication avoids copying repeated runner banners.
    seen, result = set(), []
    for line in keep:
        if line not in seen:
            seen.add(line)
            result.append(line.rstrip())
    out = "\n".join(result)
    return out[-width:]


def python_features(code: str) -> dict[str, Any]:
    features: dict[str, Any] = {
        "syntax_valid": True, "calls": [], "assertions": [], "names": [],
        "literal_values": [], "mock_mentions": [],
    }
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        features["syntax_valid"] = False
        features["syntax_error"] = f"{exc.msg} at line {exc.lineno}"
        return features

    def dotted(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            left = dotted(node.value)
            return f"{left}.{node.attr}" if left else node.attr
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = dotted(node.func)
            if name:
                features["calls"].append(name)
                if re.search(r"mock|patch|monkeypatch", name, re.I):
                    features["mock_mentions"].append(name)
        elif isinstance(node, ast.Assert):
            features["assertions"].append(ast.unparse(node.test))
        elif isinstance(node, ast.Name):
            features["names"].append(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool)):
            val = repr(node.value)
            if len(val) < 160:
                features["literal_values"].append(val)
    for call in features["calls"]:
        if re.search(r"assert|raises|expect", call, re.I):
            features["assertions"].append(call)
    for key in ("calls", "assertions", "names", "literal_values", "mock_mentions"):
        features[key] = list(dict.fromkeys(features[key]))[:80]
    return features


def target_api_names(behavior: dict[str, Any]) -> list[str]:
    names = []
    for item in behavior.get("trigger", {}).get("target_apis", []):
        name = item.get("name", "") if isinstance(item, dict) else str(item)
        if name:
            names.extend([name, name.split(".")[-1]])
    return list(dict.fromkeys(n for n in names if len(n) > 2))


def evidence_text(result: dict[str, Any]) -> str:
    return "\n".join([
        flatten_text(result.get("buggy_side_evidence", {})),
        flatten_text(result.get("surrogate_evidence", {})),
        flatten_text(result.get("selection_record", {})),
    ])


def source_for_provenance(p: dict[str, Any]) -> str:
    source = str(p.get("source", "Unknown"))
    if source == "Adaptive":
        return f"Seed{p.get('seed_index', '?')}"
    return source


def stage_record(result: dict[str, Any], candidate_path: str | None = None) -> dict[str, Any]:
    path = candidate_path or result.get("candidate_path", "")
    code = read_text(path)
    buggy_path = result.get("buggy_execution", {}).get("output_path")
    fixed_path = result.get("fixed_execution", {}).get("output_path")
    return {
        "source": result.get("source"),
        "round": result.get("round"),
        "seed_index": result.get("seed_index"),
        "candidate_id": result.get("candidate_id"),
        "sha256": result.get("sha256"),
        "candidate_path": path,
        "selected_by_brt6": bool(result.get("selected_by_brt6")),
        "provenance": result.get("provenance", []),
        "is_stage_final": any(p.get("artifact_role") == "stage_final" for p in result.get("provenance", [])),
        "buggy_status": result.get("buggy_status"),
        "fixed_status": result.get("fixed_status"),
        "error_type": result.get("error_type"),
        "execution_status": result.get("execution_status"),
        "features": python_features(code),
        "code_excerpt": code_excerpt(code),
        "buggy_trace_excerpt": trace_excerpt(read_text(buggy_path)),
        "fixed_trace_excerpt": trace_excerpt(read_text(fixed_path)),
        "verifier_evidence": compact(evidence_text(result), 1000),
    }


def trigger_score(stage: dict[str, Any], behavior: dict[str, Any]) -> tuple[int, list[str]]:
    code = read_text(stage.get("candidate_path"))
    ev = stage.get("verifier_evidence", "")
    names = target_api_names(behavior)
    score, why = 0, []
    matches = [n for n in names if re.search(rf"\b{re.escape(n)}\b", code)]
    if matches:
        score += min(3, len(matches))
        why.append("target API mentioned: " + ", ".join(matches[:4]))
    if stage.get("buggy_status") == "FAIL":
        score += 2
        why.append("buggy-side executed and failed")
    if re.search(r"正确触发|已触发|触发.*bug|target.*reach|进入.*路径", ev, re.I):
        score += 2
        why.append("verifier reports trigger evidence")
    if re.search(r"未触发|没有触发|未进入|not trigger|not reach|wrong.*path", ev, re.I):
        score -= 3
        why.append("verifier reports target not reached")
    if stage.get("buggy_status") in {"ERROR", None}:
        score -= 2
        why.append("execution error prevents behavioral evidence")
    return score, why


def keyword_hits(text: str, groups: dict[str, list[str]]) -> dict[str, int]:
    lowered = text.lower()
    return {name: sum(1 for pat in pats if re.search(pat, lowered, re.I))
            for name, pats in groups.items()}


def classify(instance: dict[str, Any]) -> dict[str, Any]:
    selected = instance["selected_candidate"]
    stages = instance["candidate_evidence"]
    behavior = instance["behavior_target"]
    issue = instance["issue"]
    all_ev = "\n".join(s.get("verifier_evidence", "") for s in stages)
    selected_code = read_text(selected.get("candidate_path"))
    selected_text = "\n".join([
        issue.get("problem_statement", ""), flatten_text(behavior), selected_code,
        selected.get("buggy_trace_excerpt", ""), selected.get("fixed_trace_excerpt", ""), all_ev,
    ])
    selected_ev_trace = "\n".join([
        selected.get("verifier_evidence", ""), selected.get("buggy_trace_excerpt", ""),
        selected.get("fixed_trace_excerpt", ""),
    ])
    statuses = [(s.get("buggy_status"), s.get("fixed_status"), s.get("error_type")) for s in stages]
    # Structural is a property of the current/best candidate, not of any abandoned
    # historical candidate.  A stage that once had an ImportError but was later made
    # executable must not poison the instance-level diagnosis.
    selected_structural_signal = bool(re.search(
        r"\b(SETUP_ERROR|COLLECT_ERROR|IMPORT_ERROR|ZERO_TEST|SyntaxError|ImportError)\b",
        selected_ev_trace, re.I))
    selected_structural_signal = selected_structural_signal and not bool(re.search(
        r"accepted\s+(ASSERTION_FAIL|PASS)|正确触发|完全一致|成功触发", selected_ev_trace, re.I))
    unmatched_semantic_transition = (selected.get("error_type") == "unmatched_transition"
                                     and selected.get("buggy_status") == "FAIL"
                                     and selected.get("fixed_status") == "PASS")
    structural = (selected.get("buggy_status") == "ERROR" or selected.get("error_type") in {
        "import_error", "zero_test", "syntax_error", "setup_error", "collection_error"}
        or selected_structural_signal or unmatched_semantic_transition)
    if not selected.get("features", {}).get("syntax_valid", True):
        structural = True

    trig_groups = {
        "wrong_mock": [r"mock.*not called", r"wrong mock", r"mock.*错误", r"patch.*错误", r"monkeypatch"],
        "wrong_call_sequence": [r"call sequence", r"调用顺序", r"state_forwards", r"before.*after", r"refresh_from_db", r"save.*refresh", r"顺序错误", r"lifecycle"],
        "wrong_fixture_setup_state": [r"fixture", r"setup", r"模型.*字段", r"foreign.?key", r"to_field", r"backend", r"配置", r"database", r"数据为空", r"没有.*数据", r"model.*field"],
        "wrong_program_state": [r"wrong state", r"状态不", r"cache", r"transaction", r"session", r"initialized", r"未初始化", r"context"],
        "wrong_input_value_boundary": [r"wrong input", r"参数.*不", r"argument", r"boundary", r"边界", r"传入.*none", r"input", r"query shape", r"empty|non.?empty"],
        "missing_api_call": [r"missing.*call", r"没有调用", r"未调用", r"缺少.*调用", r"should call"],
        "wrong_api_call": [r"wrong api", r"错误.*api", r"相邻.*api", r"instead of", r"而不是", r"deconstruct", r"makemigrations"],
        "missing_trigger_condition": [r"missing.*condition", r"缺少.*条件", r"前置条件", r"not configured", r"未设置", r"需要.*才能"],
        "target_path_not_reached": [r"未触发", r"没有触发", r"not trigger", r"未进入", r"not reach", r"target path", r"未覆盖.*路径"],
    }
    oracle_groups = {
        "wrong_exception_expectation": [r"assert.*raises", r"assertraises", r"pytest\.raises", r"exception.*expect", r"异常.*预期", r"不应.*异常", r"should not raise", r"wrong exception"],
        "wrong_observed_variable_object": [r"wrong.*object", r"观察.*错误", r"assert.*repr", r"repr\(", r"message", r"html", r"sql", r"output.*instead", r"错误.*对象"],
        "secondary_symptom_assertion": [r"secondary", r"二级症状", r"错误信息", r"error message", r"repr", r"html", r"完整.*输出", r"sql string"],
        "assertion_too_strong": [r"too strong", r"过强", r"完整.*相等", r"exact", r"brittle", r"严格", r"full.*equality"],
        "assertion_too_weak": [r"too weak", r"过弱", r"buggy.*pass", r"fixed.*pass", r"both.*pass"],
        "wrong_assertion_operator": [r"wrong.*operator", r"断言.*算子", r"contains", r"isinstance", r"not in", r"inclusion"],
        "wrong_expected_value": [r"expected.*wrong", r"wrong expected", r"期望值.*错误", r"预期.*不符", r"assertion.*fail", r"mismatch", r"expected"],
    }
    # As with Oracle subtypes, concrete candidate/trace evidence decides the subtype.
    # Issue prose is used to define target APIs above, not as a bag-of-words vote.
    trig_scores = keyword_hits(selected_code + "\n" + selected_ev_trace, trig_groups)
    # Oracle subtype must be grounded in the candidate/assertion and execution evidence.
    # The BehaviorTarget necessarily contains words such as "expected", so including it
    # here would collapse almost every Oracle diagnosis into wrong_expected_value.
    oracle_scores = keyword_hits(selected_code + "\n" + selected_ev_trace, oracle_groups)

    # Direct execution evidence gets more weight than broad Issue wording.
    for name, hits in keyword_hits(selected_ev_trace, trig_groups).items():
        trig_scores[name] += 2 * hits
    for name, hits in keyword_hits(selected_ev_trace, oracle_groups).items():
        oracle_scores[name] += 2 * hits

    # These are explicit semantic judgments made from buggy-side execution.  They are
    # materially stronger than vocabulary overlap with the Issue text.
    trigger_confirmed = bool(re.search(
        r"accepted\s+(ASSERTION_FAIL|UNRELATED_FAIL)\s+accept|正确触发|成功触发|"
        r"完全覆盖.*target|输入和调用路径.*一致|失败现象.*一致|exact target api", selected_ev_trace, re.I))
    explicitly_unrelated = bool(re.search(
        r"测试自身|与\s*Issue\s*(描述)?\s*(的)?\s*(bug|缺陷)?\s*无关|not.*issue|unrelated.*setup|"
        r"未触发|没有触发|not trigger", selected_ev_trace, re.I))
    oracle_family_bonus = 0
    trigger_family_bonus = 0
    if trigger_confirmed and not explicitly_unrelated:
        oracle_family_bonus += 9
    if re.search(r"repair_oracle|oracle\s*$|断言方向错误|oracle.*错误", selected_ev_trace, re.I):
        oracle_family_bonus += 7
    if re.search(r"断言方向错误|wrong assertion direction", selected_ev_trace, re.I):
        oracle_scores["wrong_assertion_operator"] += 5
    if re.search(r"没有断言|缺少.*断言|missing.*assert", selected_ev_trace, re.I):
        oracle_scores["assertion_too_weak"] += 6
    if re.search(r"错误信息|error message|repr|html|sql", selected_ev_trace, re.I):
        oracle_scores["secondary_symptom_assertion"] += 3
    if re.search(r"错误.*对象|错误.*状态|而非.*(状态|返回值|目标)|wrong.*object", selected_ev_trace, re.I):
        oracle_scores["wrong_observed_variable_object"] += 5
    if re.search(r"repair_trigger|触发条件|未触发|没有触发", selected_ev_trace, re.I):
        trig_scores["target_path_not_reached"] += 5
        trigger_family_bonus += 5
    if re.search(r"repair_setup|测试设置问题|fixture|基础设施问题", selected_ev_trace, re.I):
        trig_scores["wrong_fixture_setup_state"] += 5
        trigger_family_bonus += 4
    # High-specificity trigger signatures.  These are intentionally narrow and are
    # backed by the verifier's concrete account of the executed candidate.
    if re.search(r"state_forwards|database_forwards.*前|调用顺序|先调用.*再", selected_ev_trace, re.I):
        trig_scores["wrong_call_sequence"] += 14
        trigger_family_bonus += 5
    if re.search(r"mock.*未被调用|mock.*not called|mock.*错误|patch.*target.*错误", selected_ev_trace, re.I):
        trig_scores["wrong_mock"] += 14
        trigger_family_bonus += 5
    concrete_input_error = bool(re.search(
        r"传入的?\s*\w*\s*(为|是)\s*None|测试自身参数错误|wrong.*argument|"
        r"(空|非空|边界).*输入.*错误", selected_ev_trace, re.I))
    if concrete_input_error:
        trig_scores["wrong_input_value_boundary"] += 14
        trigger_family_bonus += 6
    if re.search(r"未覆盖\s*\w*[\w.]*\s*(或|/|以及)?\s*(迁移生成)?路径|"
                 r"而是直接.*(相邻|表单|choices)|wrong api|调用了错误的\s*API", selected_ev_trace, re.I):
        trig_scores["wrong_api_call"] += 14
        trigger_family_bonus += 5

    api_names = target_api_names(behavior)
    api_present = any(re.search(rf"\b{re.escape(n)}\b", selected_code) for n in api_names)
    if api_names and not api_present:
        trig_scores["missing_api_call"] += 4
        trig_scores["target_path_not_reached"] += 2
    if selected.get("buggy_status") == "PASS" and selected.get("fixed_status") == "PASS":
        trig_scores["target_path_not_reached"] += 4
        oracle_scores["assertion_too_weak"] += 2
    if selected.get("buggy_status") == "FAIL" and selected.get("fixed_status") == "FAIL":
        oracle_scores["wrong_expected_value"] += 2
    features = selected.get("features", {})
    if features.get("mock_mentions"):
        trig_scores["wrong_mock"] += 1
    assertion_text = " ".join(features.get("assertions", []))
    if len(assertion_text) > 250 or len(features.get("assertions", [])) >= 4:
        oracle_scores["assertion_too_strong"] += 2
    if re.search(r"raises|assertraises|exception", assertion_text, re.I):
        oracle_scores["wrong_exception_expectation"] += 3
    if re.search(r"assertEqual\([^\n]{120,}|assert\s+[^\n]{120,}|完整.*(字符串|输出)|exact.*(string|output)",
                 selected_code + "\n" + selected_ev_trace, re.I):
        oracle_scores["assertion_too_strong"] += 4
    if re.search(r"AssertionError:.*(!=|not in|is not)", selected.get("fixed_trace_excerpt", ""), re.I):
        oracle_scores["wrong_expected_value"] += 4

    if structural:
        family = "structural"
        subtype = "unmatched_execution_status" if unmatched_semantic_transition else "syntax_setup_import_collection"
        confidence = "high"
    else:
        t_name, t_score = max(trig_scores.items(), key=lambda x: x[1])
        o_name, o_score = max(oracle_scores.items(), key=lambda x: x[1])
        t_score += trigger_family_bonus
        o_score += oracle_family_bonus
        coarse = instance.get("coarse_failure_type", "")
        if "input_state_or_trigger" in coarse or "target_behavior_not_triggered" in coarse:
            t_score += 3
        elif "assertion_or_oracle" in coarse:
            o_score += 3
        if selected.get("buggy_status") == "PASS":
            t_score += 3
        if selected.get("buggy_status") == "FAIL" and selected.get("fixed_status") == "FAIL":
            o_score += 1
        if concrete_input_error and explicitly_unrelated:
            family, subtype = "trigger", "wrong_input_value_boundary"
        elif trigger_confirmed and not explicitly_unrelated and selected.get("fixed_status") == "FAIL":
            family, subtype = "oracle", o_name
        elif re.search(r"repair_oracle|断言方向错误", selected_ev_trace, re.I) and selected.get("fixed_status") == "FAIL":
            family, subtype = "oracle", o_name
        elif t_score > o_score + 1:
            family, subtype = "trigger", t_name
        elif o_score > t_score + 1:
            family, subtype = "oracle", o_name
        else:
            # The coarse label is audit evidence derived from code + verifier; use it only for ties.
            if "assertion_or_oracle" in coarse:
                family, subtype = "oracle", o_name
            else:
                family, subtype = "trigger", t_name
        peak = max(t_score, o_score)
        confidence = "high" if peak >= 10 else "medium" if peak >= 5 else "low"

    evidence = []
    if api_names:
        evidence.append({"kind": "target_api_static_check", "detail":
                         f"target APIs={api_names[:6]}; mentioned_in_selected_candidate={api_present}"})
    evidence.append({"kind": "execution_transition", "detail":
                     f"selected buggy={selected.get('buggy_status')}, fixed={selected.get('fixed_status')}, error={selected.get('error_type')}"})
    if selected.get("verifier_evidence"):
        evidence.append({"kind": "buggy_verifier", "detail": selected["verifier_evidence"][:700]})
    if selected.get("buggy_trace_excerpt"):
        evidence.append({"kind": "buggy_trace", "detail": selected["buggy_trace_excerpt"][-700:]})
    if selected.get("fixed_trace_excerpt"):
        evidence.append({"kind": "fixed_trace", "detail": selected["fixed_trace_excerpt"][-700:]})

    return {
        "failure_family": family,
        "fine_failure_type": subtype,
        "fine_failure_description": (TRIGGER_SUBTYPES | ORACLE_SUBTYPES).get(subtype, "结构或执行失败"),
        "confidence": confidence,
        "trigger_scores": trig_scores,
        "oracle_scores": oracle_scores,
        "evidence": evidence,
    }


def mutation_for(analysis: dict[str, Any], evolution: dict[str, Any]) -> tuple[str, str, str]:
    subtype = analysis["fine_failure_type"]
    if analysis["failure_family"] == "structural":
        return "Structural Repair", "只修语法、导入、收集与 fixture 可执行性", "测试能 collect/execute 后停止结构修复"
    if analysis["failure_family"] == "oracle":
        return "Oracle Mutation", "保护已确认的 setup、input 和目标调用路径", "fixed-side 仍 FAIL 的离线信号不允许在线使用；在线以 buggy-side 断言差异候选为止损"
    if subtype == "wrong_input_value_boundary":
        return "Input Mutation", "保护调用 API、setup 骨架和观察点", "达到目标调用且出现稳定 buggy-side 行为后停止扩展输入"
    if subtype in {"wrong_program_state", "wrong_fixture_setup_state", "wrong_mock"}:
        return "State Mutation", "保护目标 API 与核心断言意图", "fixture 可执行且目标状态已由 trace/side effect 证明后停止"
    if subtype in {"missing_api_call", "wrong_api_call", "wrong_call_sequence", "target_path_not_reached"}:
        transitions = evolution.get("all_execution_transitions", [])
        has_f2f = any(x.get("buggy") == "FAIL" and x.get("fixed") == "FAIL" for x in transitions)
        has_passpass = any(x.get("buggy") == "PASS" and x.get("fixed") == "PASS" for x in transitions)
        if evolution.get("later_repair_broke_trigger") or (has_f2f and has_passpass):
            return "Trigger then Oracle Mutation", "先冻结可执行结构；目标路径确认后冻结 input/state/call", "触发不改善则回退；确认触发后只改 observation/assertion"
        return "Call-Sequence Mutation", "保护可执行 fixture 和与 Issue 一致的输入", "目标 API/路径出现可观测 evidence 后停止改调用"
    return "Trigger then Oracle Mutation", "先保护可执行结构，触发确认后冻结 input/state/call", "触发未改善则回退；触发确认后只允许 Oracle 编辑"


def evolution_analysis(stages: list[dict[str, Any]], behavior: dict[str, Any]) -> dict[str, Any]:
    primary_all = [s for s in stages if s.get("source") in {"Round0", "Round1", "Round2", "Round3"}]
    # A round can retain rejected checkpoints in addition to its final artifact.  The
    # longitudinal path must contain one final checkpoint per round; all variants stay
    # available separately in candidate_evidence.
    by_source: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for stage in primary_all:
        by_source[stage.get("source")].append(stage)
    primary = []
    for source in ("Round0", "Round1", "Round2", "Round3"):
        choices = by_source.get(source, [])
        if choices:
            primary.append(sorted(choices, key=lambda s: (
                not s.get("is_stage_final", False), not s.get("selected_by_brt6", False), s.get("candidate_id", "")))[0])
    scores = []
    for stage in primary:
        score, why = trigger_score(stage, behavior)
        scores.append({"source": stage.get("source"), "candidate_id": stage.get("candidate_id"),
                       "score": score, "evidence": why,
                       "buggy_status": stage.get("buggy_status"), "fixed_status": stage.get("fixed_status")})
    first = next((x["source"] for x in scores if x["score"] >= 3 and x["buggy_status"] != "ERROR"), None)
    first_oracle = next((s.get("source") for s in primary
                         if s.get("buggy_status") == "FAIL" and s.get("fixed_status") == "FAIL"), None)
    broke = False
    broken_at = None
    for before, after in zip(scores, scores[1:]):
        if before["score"] >= 3 and after["score"] <= before["score"] - 3:
            broke, broken_at = True, after["source"]
            break
    trend = "unavailable"
    if len(scores) >= 2:
        delta = scores[-1]["score"] - scores[0]["score"]
        trend = "closer" if delta >= 2 else "regressed" if delta <= -2 else "flat/mixed"
    any_f2f = any(s.get("buggy_status") == "FAIL" and s.get("fixed_status") == "FAIL" for s in stages)
    any_passpass = any(s.get("buggy_status") == "PASS" and s.get("fixed_status") == "PASS" for s in stages)
    likely_assertion = any_f2f and any(x["score"] >= 3 for x in scores)
    likely_trigger = any_passpass or not likely_assertion
    return {
        "round_path": scores,
        "trigger_trend_seed_to_round3": trend,
        "first_likely_target_path_round": first,
        "first_wrong_oracle_round": first_oracle,
        "later_repair_broke_trigger": broke,
        "trigger_broken_at": broken_at,
        "likely_assertion_only_fix": likely_assertion,
        "likely_input_state_call_only_fix": likely_trigger,
        "all_execution_transitions": [{"source": s.get("source"), "buggy": s.get("buggy_status"),
                                       "fixed": s.get("fixed_status"), "error": s.get("error_type")}
                                      for s in stages],
        "note": "Path reach is inferred only from target API presence, cached execution, traces, and verifier evidence; no coverage or golden patch was consulted.",
    }


def diff_summary(before: str, after: str, limit: int = 40) -> list[str]:
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="")
    return [line for line in diff if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))][:limit]


def minimal_edit(classification: dict[str, Any], behavior: dict[str, Any]) -> str:
    subtype = classification["fine_failure_type"]
    mapping = {
        "wrong_input_value_boundary": "仅替换触发参数/边界值，使其满足 BehaviorTarget 的 trigger_condition；冻结 setup、调用 API 与断言对象。",
        "missing_trigger_condition": "补齐一个缺失前置条件；每次只引入一个条件并用 buggy-side trace 验证路径是否改变。",
        "wrong_program_state": "只构造目标调用时所需状态，保留输入与断言，避免同时重写测试。",
        "wrong_fixture_setup_state": "最小补齐 fixture/model/data/config，使目标对象具备 Issue 所需结构。",
        "missing_api_call": "插入 BehaviorTarget 指定的目标 API 调用，并直接观察其返回值或副作用。",
        "wrong_api_call": "把相邻 API 替换为目标 API；不同时修改输入和 Oracle。",
        "wrong_call_sequence": "只重排/补齐关键调用序列，在目标生命周期阶段进行观察。",
        "wrong_mock": "只修正 patch target、返回值或调用时机，并保留真实目标 API 路径。",
        "target_path_not_reached": "从当前可执行测试出发，最小修改 input/state/call，直到 buggy-side 证据确认进入目标路径。",
        "wrong_observed_variable_object": "保留 trigger，只把 observation point 改为 BehaviorTarget 指定的直接返回值/状态/副作用。",
        "wrong_expected_value": "保留 trigger 与 observation point，只替换最小 expected value；避免完整输出快照。",
        "wrong_assertion_operator": "只替换比较关系，确保断言对应 Issue 的单一语义差异。",
        "wrong_exception_expectation": "只调整异常类型/抛出阶段/是否抛出，保护触发输入与调用路径。",
        "assertion_too_strong": "删除与 Issue 无关的精确字符串、顺序或内部表示约束，只保留核心语义断言。",
        "assertion_too_weak": "增加一个直接观察目标行为的判别断言，避免只断言对象可创建或调用不报错。",
        "secondary_symptom_assertion": "从错误文本/repr/HTML/SQL 快照切换到目标 API 的直接返回值或状态副作用。",
        "syntax_setup_import_collection": "仅修语法、导入、测试发现和 fixture 构造，直到测试真实 collect/execute。",
        "unmatched_execution_status": "保留测试语义，只把失败表达为官方 harness 可识别的测试终态；不得改变 F2P 判定规则。",
    }
    return mapping.get(subtype, "保持结构可执行，每次只改变 Trigger 或 Oracle 中的一类，并在无改善时回退。")


def choose_representatives(items: list[dict[str, Any]], count: int = 15) -> list[dict[str, Any]]:
    by_type: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for item in items:
        by_type[item["classification"]["fine_failure_type"]].append(item)
    ranked_types = sorted(by_type, key=lambda k: (-len(by_type[k]), k))
    picked, used = [], set()
    preferred_ids = [
        "astropy__astropy-14182", "django__django-10924", "django__django-11910",
        "django__django-12125", "django__django-12453", "django__django-13028",
        "django__django-14999", "django__django-15388", "matplotlib__matplotlib-23314",
        "pytest-dev__pytest-5227", "sphinx-doc__sphinx-8595", "sympy__sympy-17022",
    ]
    item_by_id = {x["instance_id"]: x for x in items}
    for iid in preferred_ids:
        if iid in item_by_id and len(picked) < count:
            picked.append(item_by_id[iid]); used.add(iid)
    # First cover the dominant modes, then add high-confidence examples from them.
    for subtype in ranked_types:
        candidates = sorted(by_type[subtype], key=lambda x: (
            {"high": 0, "medium": 1, "low": 2}[x["classification"]["confidence"]],
            x["instance_id"]))
        if candidates:
            candidate = next((x for x in candidates if x["instance_id"] not in used), None)
            if candidate:
                picked.append(candidate); used.add(candidate["instance_id"])
        if len(picked) >= count:
            break
    for item in sorted(items, key=lambda x: (
            {"high": 0, "medium": 1, "low": 2}[x["classification"]["confidence"]], x["instance_id"])):
        if len(picked) >= count:
            break
        if item["instance_id"] not in used:
            picked.append(item); used.add(item["instance_id"])
    reps = []
    for item in picked:
        sel = item["selected_candidate"]
        reps.append({
            "instance_id": item["instance_id"],
            "failure_type": item["classification"]["fine_failure_type"],
            "confidence": item["classification"]["confidence"],
            "original_seed": {
                "test_file": item["seed"].get("test_file"),
                "test_name": item["seed"].get("test_name"),
                "code_excerpt": code_excerpt(item["seed"].get("code", ""), 1200),
            },
            "current_erroneous_candidate": {
                "source": sel.get("source"), "candidate_id": sel.get("candidate_id"),
                "code_excerpt": sel.get("code_excerpt"),
            },
            "why_failed": item["classification"]["evidence"][:5],
            "minimal_next_edit": item["minimal_next_edit"],
        })
    return reps


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=Path("results/runs") / RUN_NAME)
    parser.add_argument("--issues", type=Path, default=Path("data/issues/swt276_issues.json"))
    args = parser.parse_args()
    run = args.run_dir.resolve()
    audit = run / "evaluation/golden_oracle_audit"
    out = run / "evaluation/generation_miss_analysis"
    out.mkdir(parents=True, exist_ok=True)

    miss_doc = load_json(audit / "generation_misses.json", {})
    misses = {x["instance_id"]: x.get("failure_type", "unknown") for x in miss_doc.get("instances", [])}
    issues = {x["instance_id"]: x for x in load_json(args.issues, [])}
    results_by_instance: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    with (audit / "candidate_results.jsonl").open(errors="replace") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("instance_id") in misses:
                results_by_instance[row["instance_id"]].append(row)

    records = []
    for iid in sorted(misses):
        generation = run / "generation" / iid
        behavior = load_json(generation / "behavior_target.json", {}) or {}
        host = load_json(generation / "host_context.json", {}) or {}
        summary = load_json(generation / "summary.json", {}) or {}
        mutation_plan = load_json(generation / "mutation_round_0_plan.json", {}) or {}
        raw_results = results_by_instance.get(iid, [])
        selected_raw = next((r for r in raw_results if r.get("selected_by_brt6")), None)
        if selected_raw is None:
            # Generation miss means no candidate is F2P; use the latest verified candidate as current.
            selected_raw = sorted(raw_results, key=lambda r: SOURCE_ORDER.get(r.get("source"), 99))[-1]
        stages = [stage_record(r) for r in sorted(raw_results,
                  key=lambda r: (SOURCE_ORDER.get(r.get("source"), 99), r.get("candidate_id", "")))]
        selected = next((s for s in stages if s["candidate_id"] == selected_raw.get("candidate_id")),
                        stage_record(selected_raw))
        evo = evolution_analysis(stages, behavior)
        record: dict[str, Any] = {
            "instance_id": iid,
            "repo": issues.get(iid, {}).get("repo"),
            "coarse_failure_type": misses[iid],
            "issue": issues.get(iid, {}),
            "behavior_target": behavior,
            "seed": {
                "test_file": host.get("full_test_file_path") or host.get("host_file") or summary.get("selected_seed_file"),
                "test_name": host.get("seed_test_name") or summary.get("selected_seed_name"),
                "code": host.get("seed_test_code", ""),
                "retrieved_seeds": behavior.get("setup", {}).get("related_test_seeds", []),
            },
            "mutation_plan": mutation_plan,
            "selected_candidate": selected,
            "all_candidate_ids": [s["candidate_id"] for s in stages],
            "candidate_evidence": stages,
            "evolution": evo,
        }
        record["classification"] = classify(record)
        family, protected, stop = mutation_for(record["classification"], evo)
        record["recommended_mutation"] = {
            "family": family, "protected_parts": protected,
            "allowed_edits": minimal_edit(record["classification"], behavior),
            "stop_or_fallback_condition": stop,
        }
        # Make the two "only" flags mutually interpretable rather than optimistic
        # transition heuristics.  They now follow the final trace-grounded routing.
        evo["likely_assertion_only_fix"] = family == "Oracle Mutation"
        evo["likely_input_state_call_only_fix"] = family in {
            "Input Mutation", "State Mutation", "Call-Sequence Mutation"}
        if record["classification"]["failure_family"] != "oracle" and family != "Trigger then Oracle Mutation":
            evo["first_wrong_oracle_round"] = None
        record["minimal_next_edit"] = record["recommended_mutation"]["allowed_edits"]
        path_ids = {x["candidate_id"] for x in evo["round_path"]}
        primary = [s for s in stages if s.get("candidate_id") in path_ids]
        primary.sort(key=lambda s: SOURCE_ORDER.get(s.get("source"), 99))
        record["round_diffs"] = []
        prev_name, prev_code = "Seed", host.get("seed_test_code", "")
        for stage in primary:
            now_code = read_text(stage.get("candidate_path"))
            record["round_diffs"].append({"from": prev_name, "to": stage.get("source"),
                                          "changed_lines": diff_summary(prev_code, now_code)})
            prev_name, prev_code = stage.get("source"), now_code
        records.append(record)

    fine_counts = collections.Counter(r["classification"]["fine_failure_type"] for r in records)
    family_counts = collections.Counter(r["classification"]["failure_family"] for r in records)
    mutation_counts = collections.Counter(r["recommended_mutation"]["family"] for r in records)
    confidence_counts = collections.Counter(r["classification"]["confidence"] for r in records)
    coarse_fine: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for r in records:
        coarse_fine[r["coarse_failure_type"]][r["classification"]["fine_failure_type"]] += 1
    total = len(records)
    detailed_taxonomy = {
        "schema_version": "generation-miss-taxonomy.v1",
        "total_instances": total,
        "constraints": {
            "model_calls": 0, "test_executions": 0, "golden_patch_content_read": False,
            "inputs": ["Issue", "BehaviorTarget", "seed/candidate code", "cached buggy/fixed traces",
                       "verifier/surrogate/ranking metadata"],
        },
        "families": {
            "trigger": TRIGGER_SUBTYPES,
            "oracle": ORACLE_SUBTYPES,
            "structural": {
                "syntax_setup_import_collection": "语法、导入、setup 或 collection 阻止真实行为验证",
                "unmatched_execution_status": "执行呈现 FAIL/PASS 语义，但官方 terminal status 未匹配，不能按正式 F2P 计数",
            },
        },
        "counts": dict(fine_counts),
        "family_counts": dict(family_counts),
        "original_audit_category_breakdown": {k: dict(v.most_common()) for k, v in coarse_fine.items()},
        "classification_method": [
            "Use Issue/BehaviorTarget to identify trigger API, condition, observation point and expected behavior.",
            "Parse candidate AST for calls, assertions, literals and mocks.",
            "Use cached buggy/fixed traces plus verifier/surrogate evidence to distinguish path, oracle and infrastructure failures.",
            "Use coarse audit label only as tie-break evidence; never classify from return code alone.",
            "No golden patch content or fixed implementation source is inspected.",
        ],
    }
    (out / "detailed_taxonomy.json").write_text(json.dumps(detailed_taxonomy, ensure_ascii=False, indent=2) + "\n")
    with (out / "per_instance_analysis.jsonl").open("w") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    mode_descriptions = TRIGGER_SUBTYPES | ORACLE_SUBTYPES | {
        "syntax_setup_import_collection": "结构/环境阻止候选被执行",
        "unmatched_execution_status": "buggy FAIL / fixed PASS 已出现，但官方测试终态未被识别",
    }
    top5 = []
    for subtype, count in fine_counts.most_common(5):
        sample = next(r for r in records if r["classification"]["fine_failure_type"] == subtype)
        top5.append({
            "failure_type": subtype, "description": mode_descriptions[subtype],
            "count": count, "rate": count / total,
            "best_mutation": sample["recommended_mutation"]["family"],
            "allowed_edits": sample["recommended_mutation"]["allowed_edits"],
        })
    mutation_priority = []
    for family, count in mutation_counts.most_common():
        examples = [r["instance_id"] for r in records if r["recommended_mutation"]["family"] == family][:8]
        mutation_priority.append({"mutation": family, "theoretical_coverage": count,
                                  "rate": count / total, "example_instances": examples,
                                  "note": "Coverage is an upper bound from diagnosis, not an expected F2P gain; categories are mutually exclusive."})
    trend_counts = collections.Counter(r["evolution"]["trigger_trend_seed_to_round3"] for r in records)
    first_path_counts = collections.Counter(r["evolution"]["first_likely_target_path_round"] or "not_observed"
                                            for r in records)
    first_oracle_counts = collections.Counter(r["evolution"]["first_wrong_oracle_round"] or "not_observed"
                                              for r in records)
    broke_ids = [r["instance_id"] for r in records if r["evolution"]["later_repair_broke_trigger"]]
    source_instance_counts: dict[str, set[str]] = collections.defaultdict(set)
    source_candidate_counts = collections.Counter()
    for r in records:
        for stage in r["candidate_evidence"]:
            source_instance_counts[str(stage.get("source"))].add(r["instance_id"])
            source_candidate_counts[str(stage.get("source"))] += 1
    summary = {
        "schema_version": "generation-miss-analysis.v1",
        "total_generation_misses": total,
        "baseline_context": {"selected_f2p": 132, "oracle_portfolio_f2p": 153,
                             "selection_miss": 21, "generation_miss": 123},
        "fine_failure_types": {k: {"count": v, "rate": v / total,
                                             "description": mode_descriptions[k]}
                               for k, v in fine_counts.most_common()},
        "failure_families": {k: {"count": v, "rate": v / total} for k, v in family_counts.items()},
        "requested_parent_category_breakdown": {
            "错误输入、状态或未正确触发目标行为导致 F2F (original 54)":
                dict(coarse_fine.get("F2F_wrong_input_state_or_trigger", {})),
            "错误断言或错误 Oracle 导致 F2F (original 34)":
                dict(coarse_fine.get("F2F_wrong_assertion_or_oracle", {})),
            "note": "The original buckets are retained for auditability. Fine diagnosis may reveal a cross-cutting Oracle or Trigger root cause and therefore does not force the old label.",
        },
        "confidence": dict(confidence_counts),
        "candidate_inventory": {
            source: {"instances": len(source_instance_counts[source]), "unique_candidates": source_candidate_counts[source]}
            for source in sorted(source_instance_counts, key=lambda x: SOURCE_ORDER.get(x, 99))
        },
        "round_evolution": {
            "trigger_trend": dict(trend_counts),
            "first_likely_target_path_round": dict(first_path_counts),
            "first_wrong_oracle_round": dict(first_oracle_counts),
            "later_repair_broke_trigger_count": len(broke_ids),
            "later_repair_broke_trigger_instances": broke_ids,
            "assertion_only_fix_plausible": sum(r["evolution"]["likely_assertion_only_fix"] for r in records),
            "input_state_call_only_fix_plausible": sum(r["evolution"]["likely_input_state_call_only_fix"] for r in records),
        },
        "top_5_failure_patterns": top5,
        "recommended_mutation_assignment": {
            "Input Mutation": mutation_counts.get("Input Mutation", 0),
            "State Mutation": mutation_counts.get("State Mutation", 0),
            "Call-Sequence Mutation": mutation_counts.get("Call-Sequence Mutation", 0),
            "Oracle Mutation": mutation_counts.get("Oracle Mutation", 0),
            "Trigger then Oracle Mutation": mutation_counts.get("Trigger then Oracle Mutation", 0),
            "Structural Repair": mutation_counts.get("Structural Repair", 0),
        },
        "priority_mutations": mutation_priority[:5],
        "representative_instances": choose_representatives(records, 15),
        "mutation_controller": {
            "format": "failure signal → mutation family → protected parts → allowed edits → stop/fallback condition",
            "rules": [
                {"failure_signal": "PASS/PASS or target API/path absent", "mutation_family": "Input/State/Call-Sequence Mutation",
                 "protected_parts": "collectable structure and any already verified setup", "allowed_edits": "one trigger slot per attempt",
                 "stop_fallback": "path evidence improves: freeze trigger; no improvement: revert and try next family"},
                {"failure_signal": "FAIL/FAIL after target path evidence", "mutation_family": "Oracle Mutation",
                 "protected_parts": "input, state, target call sequence", "allowed_edits": "observation point, expected relation/value only",
                 "stop_fallback": "buggy-side discriminatory failure remains; regression to PASS/ERROR: revert"},
                {"failure_signal": "syntax/import/setup/zero-test", "mutation_family": "Structural Repair",
                 "protected_parts": "BehaviorTarget and semantic assertion intent", "allowed_edits": "syntax, imports, fixture plumbing, discovery",
                 "stop_fallback": "test collects and executes once; then hand back to semantic mutation"},
            ],
            "online_constraint": "All signals usable online must come from Issue + buggy-side evidence. Fixed/golden evidence here is diagnostic only and must not enter generation, ranking, or stopping decisions.",
        },
        "limitations": [
            "This is post-hoc causal diagnosis from generated code and cached traces, not proof that one edit will yield F2P.",
            "Target-path reach is inferred from static API references, trace text and verifier evidence because coverage was not rerun.",
            "Theoretical coverage is a mutually exclusive routing count, not additive expected benchmark improvement.",
        ],
    }
    assigned = sum(summary["recommended_mutation_assignment"].values())
    if total != 123 or assigned != total:
        raise SystemExit(f"integrity failure: total={total}, assigned={assigned}")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output_dir": str(out), "total": total, "fine_counts": fine_counts,
                      "mutation_counts": mutation_counts, "confidence": confidence_counts},
                     ensure_ascii=False, indent=2, default=dict))


if __name__ == "__main__":
    main()
