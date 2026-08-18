from __future__ import annotations

import unittest

from brt6.mutation.failure_directed import (
    MutationFamily,
    enforce_protection,
    is_execution_regression,
    route_mutation,
)


BEHAVIOR = {
    "trigger": {
        "target_apis": [
            {"name": "target_api", "source_path": "pkg/target.py"},
        ],
        "suspected_bug_locations": [{"path": "pkg/target.py"}],
    }
}


class FailureDirectedRouteTests(unittest.TestCase):
    def test_structural_route(self) -> None:
        route = route_mutation(BEHAVIOR, "def test_x(): pass", "SYNTAX_ERROR", "SyntaxError", {})
        self.assertEqual(route.family, MutationFamily.STRUCTURAL_REPAIR)

    def test_reached_failure_routes_oracle(self) -> None:
        route = route_mutation(
            BEHAVIOR,
            "def test_x():\n    assert target_api(1) == 2\n",
            "ASSERTION_FAIL",
            "target_api\nAssertionError",
            {"reason": "已触达目标 API，失败现象一致"},
        )
        self.assertEqual(route.family, MutationFamily.ORACLE_MUTATION)
        self.assertTrue(route.target_path_reached)

    def test_state_input_call_sequence_routes(self) -> None:
        state = route_mutation(BEHAVIOR, "target_api()", "PASS", "1 passed", {"reason": "fixture 配置状态缺失"})
        inp = route_mutation(BEHAVIOR, "target_api()", "PASS", "1 passed", {"reason": "输入参数边界错误"})
        seq = route_mutation(BEHAVIOR, "target_api()", "PASS", "1 passed", {"reason": "调用顺序错误，需要先调用 state_forwards"})
        self.assertEqual(state.family, MutationFamily.STATE_MUTATION)
        self.assertEqual(inp.family, MutationFamily.INPUT_MUTATION)
        self.assertEqual(seq.family, MutationFamily.CALL_SEQUENCE_MUTATION)

    def test_trigger_to_oracle_switch(self) -> None:
        route = route_mutation(
            BEHAVIOR, "target_api(1)", "ASSERTION_FAIL", "target_api AssertionError",
            {"reason": "成功触发目标路径"},
            {"mutation_family": "INPUT_MUTATION"},
        )
        self.assertEqual(route.family, MutationFamily.ORACLE_MUTATION)
        self.assertTrue(route.trigger_to_oracle)


class MutationProtectionTests(unittest.TestCase):
    def test_oracle_allows_only_assertion_edit(self) -> None:
        parent = "def test_x():\n    value = target_api(1)\n    assert value == 2\n"
        child = "def test_x():\n    value = target_api(1)\n    assert value != 3\n"
        result = enforce_protection(MutationFamily.ORACLE_MUTATION, parent, child, BEHAVIOR)
        self.assertTrue(result.accepted)
        self.assertTrue(result.oracle_changed)
        self.assertFalse(result.trigger_changed)

    def test_oracle_rejects_trigger_change(self) -> None:
        parent = "def test_x():\n    value = target_api(1)\n    assert value == 2\n"
        child = "def test_x():\n    value = target_api(9)\n    assert value == 2\n"
        result = enforce_protection(MutationFamily.ORACLE_MUTATION, parent, child, BEHAVIOR)
        self.assertFalse(result.accepted)
        self.assertIn("Oracle mutation changed input/setup/target call expressions", result.violations)

    def test_state_rejects_oracle_change(self) -> None:
        parent = "def test_x():\n    obj = make(1)\n    assert target_api(obj) == 2\n"
        child = "def test_x():\n    obj = make(config=True)\n    assert target_api(obj) == 3\n"
        result = enforce_protection(MutationFamily.STATE_MUTATION, parent, child, BEHAVIOR)
        self.assertFalse(result.accepted)
        self.assertIn("State mutation changed Oracle semantics", result.violations)

    def test_structural_allows_import_only(self) -> None:
        parent = "def test_x():\n    assert target_api(1) == 2\n"
        child = "import pytest\n\ndef test_x():\n    assert target_api(1) == 2\n"
        self.assertTrue(enforce_protection(
            MutationFamily.STRUCTURAL_REPAIR, parent, child, BEHAVIOR
        ).accepted)

    def test_input_and_call_sequence_are_limited(self) -> None:
        parent = "def test_x():\n    setup()\n    value = target_api(1)\n    assert value == 2\n"
        input_child = "def test_x():\n    setup()\n    value = target_api(0)\n    assert value == 2\n"
        seq_child = "def test_x():\n    value = target_api(1)\n    setup()\n    assert value == 2\n"
        self.assertTrue(enforce_protection(
            MutationFamily.INPUT_MUTATION, parent, input_child, BEHAVIOR
        ).accepted)
        self.assertTrue(enforce_protection(
            MutationFamily.CALL_SEQUENCE_MUTATION, parent, seq_child, BEHAVIOR
        ).accepted)

    def test_execution_regressions_fallback(self) -> None:
        self.assertEqual(
            is_execution_regression(
                {"target_path_before": True, "execution_status_before": "ASSERTION_FAIL", "mutation_family": "ORACLE_MUTATION"},
                "PASS", False,
            ),
            "target_path_lost",
        )
        self.assertEqual(
            is_execution_regression(
                {"target_path_before": True, "execution_status_before": "ASSERTION_FAIL", "mutation_family": "ORACLE_MUTATION"},
                "PASS", True,
            ),
            "oracle_mutation_regressed_buggy_fail_to_unrelated_pass",
        )


if __name__ == "__main__":
    unittest.main()
