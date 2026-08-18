"""Failure-Directed Mutation Controller V1.

The controller is intentionally buggy-side only.  It routes a cached official
execution to one mutation family, describes the editable/protected regions,
and rejects candidates that escape that contract.  It never reads a golden
patch, fixed-side execution, or F2P label.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import re
from typing import Any


HARD_FAILURES = {"SYNTAX_ERROR", "SETUP_ERROR", "COLLECT_ERROR", "IMPORT_ERROR", "ZERO_TEST"}
EXECUTABLE_FAILURES = {"ASSERTION_FAIL", "ISSUE_ALIGNED_FAIL", "RUNTIME_ERROR", "ERROR"}


class MutationFamily(str, Enum):
    STRUCTURAL_REPAIR = "STRUCTURAL_REPAIR"
    ORACLE_MUTATION = "ORACLE_MUTATION"
    STATE_MUTATION = "STATE_MUTATION"
    CALL_SEQUENCE_MUTATION = "CALL_SEQUENCE_MUTATION"
    INPUT_MUTATION = "INPUT_MUTATION"


@dataclass(frozen=True)
class MutationRoute:
    family: MutationFamily
    failure_signal: str
    target_path_reached: bool
    trigger_to_oracle: bool = False
    editable_regions: tuple[str, ...] = ()
    protected_regions: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutation_family": self.family.value,
            "failure_signal": self.failure_signal,
            "target_path_reached": self.target_path_reached,
            "trigger_to_oracle": self.trigger_to_oracle,
            "editable_regions": list(self.editable_regions),
            "protected_regions": list(self.protected_regions),
            "evidence": list(self.evidence),
        }


@dataclass
class ProtectionResult:
    accepted: bool
    violations: list[str] = field(default_factory=list)
    trigger_changed: bool = False
    oracle_changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "violations": self.violations,
            "trigger_changed": self.trigger_changed,
            "oracle_changed": self.oracle_changed,
        }


def _behavior_dict(behavior: Any) -> dict[str, Any]:
    if isinstance(behavior, dict):
        return behavior
    if hasattr(behavior, "to_dict"):
        return behavior.to_dict()
    return {}


def target_api_names(behavior: Any) -> list[str]:
    payload = _behavior_dict(behavior)
    trigger = payload.get("trigger") or payload.get("trigger_condition") or {}
    raw = trigger.get("target_apis", []) if isinstance(trigger, dict) else []
    names: list[str] = []
    for item in raw:
        name = item.get("name", "") if isinstance(item, dict) else str(item)
        if name:
            names.extend((name, name.rsplit(".", 1)[-1]))
    return list(dict.fromkeys(name for name in names if len(name) > 2))


def target_source_paths(behavior: Any) -> list[str]:
    payload = _behavior_dict(behavior)
    trigger = payload.get("trigger") or payload.get("trigger_condition") or {}
    if not isinstance(trigger, dict):
        return []
    paths = []
    for item in trigger.get("target_apis", []):
        if isinstance(item, dict) and item.get("source_path"):
            paths.append(str(item["source_path"]).lstrip("/"))
    for item in trigger.get("suspected_bug_locations", []):
        if isinstance(item, dict) and item.get("path"):
            paths.append(str(item["path"]).lstrip("/"))
    return list(dict.fromkeys(paths))


def _decision_text(decision: Any) -> str:
    if isinstance(decision, dict):
        return json.dumps(decision, ensure_ascii=False)
    values = [
        str(getattr(decision, "decision", "")),
        str(getattr(decision, "reason", "")),
        str(getattr(decision, "next_action", "")),
        " ".join(str(x) for x in getattr(decision, "focus", []) or []),
    ]
    return " ".join(values)


def infer_target_path(
    behavior: Any,
    candidate_code: str,
    execution_text: str,
    decision: Any,
) -> tuple[bool, list[str]]:
    """Infer path reach from buggy-side evidence only.

    A static API mention is not sufficient by itself.  We require verifier
    evidence, a target source path with positive coverage, or an executed
    failure whose traceback names a target API.
    """
    evidence: list[str] = []
    decision_text = _decision_text(decision)
    api_names = target_api_names(behavior)
    source_paths = target_source_paths(behavior)
    low_log = execution_text.lower()
    if re.search(
        r"正确触发|成功触发|已触达|触发了目标|输入和调用路径.*一致|"
        r"exact target api|target path reached|failure.*aligned",
        decision_text,
        re.I,
    ):
        evidence.append("buggy-side verifier confirms target path")
    # SWT logs dump coverage as path -> line counts.  Requiring a positive
    # count near the target path avoids treating a mere include-pattern banner
    # as coverage evidence.
    for path in source_paths:
        basename = path.rsplit("/", 1)[-1]
        pattern = rf"[^\n]*{re.escape(basename)}[^\n]*\{{[^\n]*:\s*[1-9]\d*"
        if re.search(pattern, execution_text, re.I):
            evidence.append(f"positive buggy-side coverage in {path}")
            break
    mentioned = [name for name in api_names if name in candidate_code]
    traced = [name for name in api_names if re.search(rf"\b{re.escape(name)}\b", execution_text)]
    if mentioned and traced:
        evidence.append("target API appears in candidate and buggy traceback")
    contradicted = bool(re.search(
        r"未触发|没有触发|未触达|未进入|not trigger|not reach|wrong path",
        decision_text, re.I
    ))
    if contradicted:
        evidence.append("buggy-side verifier reports target path not reached")
        return False, evidence
    return bool(evidence), evidence


def route_mutation(
    behavior: Any,
    candidate_code: str,
    execution_status: str,
    execution_text: str,
    decision: Any,
    parent_provenance: dict[str, Any] | None = None,
) -> MutationRoute:
    reached, evidence = infer_target_path(
        behavior, candidate_code, execution_text, decision
    )
    status = str(execution_status or "").upper()
    decision_text = _decision_text(decision)
    previous_family = str((parent_provenance or {}).get("mutation_family") or "")
    switched = reached and previous_family in {
        MutationFamily.STATE_MUTATION.value,
        MutationFamily.CALL_SEQUENCE_MUTATION.value,
        MutationFamily.INPUT_MUTATION.value,
    }

    if status in HARD_FAILURES:
        family = MutationFamily.STRUCTURAL_REPAIR
        signal = f"non-executable buggy-side result: {status}"
    elif reached and status not in HARD_FAILURES | {"PASS", "BUGGY_PASS"}:
        family = MutationFamily.ORACLE_MUTATION
        signal = "target path reached and buggy-side execution fails"
    else:
        text = decision_text.lower()
        if re.search(r"mock|fixture|setup state|program state|configuration|配置|状态|对象构造|前置条件", text):
            family = MutationFamily.STATE_MUTATION
            signal = "target path missing because fixture/object/config/state is incomplete"
        elif re.search(r"call sequence|调用顺序|state_forwards|database_forwards|先调用|调用链|lifecycle", text):
            family = MutationFamily.CALL_SEQUENCE_MUTATION
            signal = "target path missing because target call sequence is wrong"
        elif re.search(r"input|argument|boundary|参数|边界|传入|none|空列表|非空", text):
            family = MutationFamily.INPUT_MUTATION
            signal = "target path missing because trigger input/boundary is wrong"
        else:
            # The lightweight default is state, because it can repair the
            # precondition without authorizing a whole-test rewrite.
            family = MutationFamily.STATE_MUTATION
            signal = "target path not confirmed; repair minimal precondition/state"

    editable, protected = mutation_contract(family)
    return MutationRoute(
        family=family,
        failure_signal=signal,
        target_path_reached=reached,
        trigger_to_oracle=switched and family == MutationFamily.ORACLE_MUTATION,
        editable_regions=editable,
        protected_regions=protected,
        evidence=tuple(evidence),
    )


def mutation_contract(family: MutationFamily) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if family == MutationFamily.ORACLE_MUTATION:
        return (
            ("observation", "assertion", "expected_value", "assertion_operator", "expected_exception"),
            ("input", "fixture", "setup", "program_state", "target_api", "call_sequence"),
        )
    if family == MutationFamily.STATE_MUTATION:
        return (
            ("fixture", "object_construction", "configuration", "mock_state", "precondition"),
            ("target_api", "assertion_intent", "unrelated_test_structure"),
        )
    if family == MutationFamily.STRUCTURAL_REPAIR:
        return (
            ("syntax", "imports", "fixture_resolution", "collection", "test_discovery", "framework_compatibility"),
            ("trigger_semantics", "oracle_semantics"),
        )
    if family == MutationFamily.INPUT_MUTATION:
        return (
            ("trigger_arguments", "boundary_values", "input_objects"),
            ("fixture", "target_api", "call_sequence", "oracle_semantics"),
        )
    return (
        ("target_api_calls", "target_call_order"),
        ("fixture", "input_values", "oracle_semantics", "unrelated_test_structure"),
    )


def contract_prompt(route: MutationRoute) -> dict[str, Any]:
    return {
        **route.to_dict(),
        "instruction": (
            "Make the smallest possible edit. Do not redesign or freely rewrite the test. "
            "Every protected region must remain semantically unchanged. Return one complete Python file."
        ),
        "violation_policy": "reject; one constrained retry; then fall back to parent",
    }


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        left = _dotted(node.value)
        return f"{left}.{node.attr}" if left else node.attr
    return ""


def _is_oracle_call(name: str) -> bool:
    return bool(re.search(
        r"(^|\.)(assert\w*|raises|warns|fail|snapshot|match|assertlogs)$", name, re.I
    ))


def _oracle_nodes(tree: ast.AST) -> list[str]:
    nodes: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            nodes.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            names = [_dotted(item.context_expr.func) for item in node.items
                     if isinstance(item.context_expr, ast.Call)]
            if any(_is_oracle_call(name) for name in names):
                nodes.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.Call) and _is_oracle_call(_dotted(node.func)):
            nodes.append(ast.dump(node, include_attributes=False))
    return sorted(set(nodes))


def _call_features(tree: ast.AST) -> tuple[list[str], list[str], list[str]]:
    names, full, literals = [], [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if not _is_oracle_call(name):
                names.append(name)
                full.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
            literals.append(repr(node.value))
    return names, full, literals


def _target_calls(tree: ast.AST, behavior: Any) -> list[str]:
    targets = set(target_api_names(behavior))
    result = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in targets or name.rsplit(".", 1)[-1] in targets:
                result.append(ast.dump(node, include_attributes=False))
    return result


def _fingerprints(code: str, behavior: Any) -> dict[str, Any]:
    tree = ast.parse(code)
    names, calls, literals = _call_features(tree)
    return {
        "oracle": _oracle_nodes(tree),
        "call_names": names,
        "calls": calls,
        "literals": literals,
        "target_calls": _target_calls(tree, behavior),
    }


def enforce_protection(
    family: MutationFamily,
    parent_code: str,
    child_code: str,
    behavior: Any,
) -> ProtectionResult:
    try:
        parent = _fingerprints(parent_code, behavior)
        child = _fingerprints(child_code, behavior)
    except SyntaxError as exc:
        return ProtectionResult(False, [f"child syntax invalid: {exc}"], False, False)
    oracle_changed = parent["oracle"] != child["oracle"]
    trigger_changed = (
        parent["call_names"] != child["call_names"]
        or parent["target_calls"] != child["target_calls"]
        or parent["calls"] != child["calls"]
    )
    violations: list[str] = []
    if family == MutationFamily.ORACLE_MUTATION:
        if parent["calls"] != child["calls"]:
            violations.append("Oracle mutation changed input/setup/target call expressions")
        if parent["call_names"] != child["call_names"]:
            violations.append("Oracle mutation changed call sequence")
    elif family == MutationFamily.STATE_MUTATION:
        if oracle_changed:
            violations.append("State mutation changed Oracle semantics")
        if [re.sub(r"\(.*", "", x) for x in parent["target_calls"]] != [
            re.sub(r"\(.*", "", x) for x in child["target_calls"]
        ]:
            violations.append("State mutation removed or replaced a target API")
    elif family == MutationFamily.STRUCTURAL_REPAIR:
        if oracle_changed:
            violations.append("Structural repair changed Oracle semantics")
        if parent["target_calls"] != child["target_calls"]:
            violations.append("Structural repair changed Trigger semantics")
    elif family == MutationFamily.INPUT_MUTATION:
        if oracle_changed:
            violations.append("Input mutation changed Oracle semantics")
        if parent["call_names"] != child["call_names"]:
            violations.append("Input mutation changed call sequence/API")
    elif family == MutationFamily.CALL_SEQUENCE_MUTATION:
        if oracle_changed:
            violations.append("Call-sequence mutation changed Oracle semantics")
        if parent["literals"] != child["literals"]:
            violations.append("Call-sequence mutation changed input literals")
    return ProtectionResult(not violations, violations, trigger_changed, oracle_changed)


def candidate_id(instance_id: str, code: str) -> str:
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:20]
    return f"{instance_id}__{digest}"


def is_execution_regression(
    parent_provenance: dict[str, Any] | None,
    current_status: str,
    target_path_after: bool,
) -> str:
    if not parent_provenance:
        return ""
    before_status = str(parent_provenance.get("execution_status_before") or "")
    before_target = bool(parent_provenance.get("target_path_before"))
    family = str(parent_provenance.get("mutation_family") or "")
    current = str(current_status or "").upper()
    if before_target and not target_path_after:
        return "target_path_lost"
    if before_status not in HARD_FAILURES and current in HARD_FAILURES:
        return "executable_candidate_regressed_to_error"
    if family == MutationFamily.ORACLE_MUTATION.value and before_status in EXECUTABLE_FAILURES and current == "PASS":
        return "oracle_mutation_regressed_buggy_fail_to_unrelated_pass"
    return ""
