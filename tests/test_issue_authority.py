from __future__ import annotations

from brt6.core.schema import BehaviorTarget
from brt6.issue.issue_rewriter import (
    apply_issue_authority_constraints,
    behavior_from_dict,
)


def test_nested_behavior_target_preserves_oracle_for_docker_feedback() -> None:
    behavior = behavior_from_dict(
        "django__django-1",
        {
            "schema_version": "behavior_target.lossless.v1",
            "instance_id": "django__django-1",
            "setup": {"setup_hints": [{"hint": "database"}]},
            "trigger": {"trigger_condition": {"text": "call distinct"}},
            "oracle": {
                "expected_behavior": {"text": "raise NotSupportedError"},
                "assertion_hints": [{"preferred_assertion_style": "raises"}],
            },
        },
    )
    assert behavior.trigger_condition["text"] == "call distinct"
    assert behavior.expected_behavior["text"] == "raise NotSupportedError"
    assert behavior.assertion_hints[0]["preferred_assertion_style"] == "raises"


def test_explicit_title_contract_overrides_reporter_success_hypothesis() -> None:
    behavior = BehaviorTarget(
        "django__django-12908",
        expected_behavior={"text": "The wrapped union should return two rows."},
        assertion_hints=[{"preferred_assertion_style": "equals"}],
    )
    audited = apply_issue_authority_constraints(
        "Union queryset should raise on distinct().\n"
        "Description\nexpected to get wrapped union and two rows",
        behavior,
    )
    assert audited.expected_behavior["authority"] == "issue_title_contract"
    assert "raise/reject" in audited.expected_behavior["text"]
    assert audited.assertion_hints[0]["preferred_assertion_style"] == "raises"
    assert any("wrapped union" in item for item in audited.uncertainties)


def test_non_contract_title_is_left_unchanged() -> None:
    behavior = BehaviorTarget(
        "sympy__sympy-1", expected_behavior={"text": "return the simplified value"}
    )
    audited = apply_issue_authority_constraints(
        "Incorrect simplification for Piecewise", behavior
    )
    assert audited.expected_behavior["text"] == "return the simplified value"
