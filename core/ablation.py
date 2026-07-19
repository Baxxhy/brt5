"""Normalized, mutually exclusive ablation configuration."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any


_COMPONENTS = (
    "behavior_target",
    "mutation",
    "specialized_feedback",
    "environment_feedback",
    "trigger_feedback",
    "assertion_feedback",
)

_VARIANTS = {
    "behavior_target": ("wo_behavior_target", "w/o Behavior Target", "_wo_behavior_target"),
    "mutation": ("wo_mutation", "w/o Mutation", "_wo_mutation"),
    "specialized_feedback": (
        "generic_iteration",
        "Generic Iteration",
        "_generic_iteration",
    ),
    "environment_feedback": (
        "wo_environment_feedback",
        "w/o Environment Feedback",
        "_wo_environment_feedback",
    ),
    "trigger_feedback": (
        "wo_trigger_feedback",
        "w/o Trigger Feedback",
        "_wo_trigger_feedback",
    ),
    "assertion_feedback": (
        "wo_assertion_feedback",
        "w/o Assertion Feedback",
        "_wo_assertion_feedback",
    ),
}


@dataclass(frozen=True)
class AblationConfig:
    """One-factor-at-a-time component controls for generation experiments."""

    behavior_target: bool = True
    mutation: bool = True
    specialized_feedback: bool = True
    environment_feedback: bool = True
    trigger_feedback: bool = True
    assertion_feedback: bool = True

    def disabled_components(self) -> list[str]:
        return [name for name in _COMPONENTS if not bool(getattr(self, name))]

    def validate(self) -> "AblationConfig":
        disabled = self.disabled_components()
        if len(disabled) > 1:
            raise ValueError(
                "ablation switches are mutually exclusive; disable exactly one "
                "component per run, got: " + ", ".join(disabled)
            )
        return self

    @property
    def is_ablation(self) -> bool:
        return bool(self.disabled_components())

    @property
    def disabled_component(self) -> str:
        disabled = self.disabled_components()
        return disabled[0] if disabled else ""

    @property
    def ablation_id(self) -> str:
        if not self.disabled_component:
            return "full"
        return _VARIANTS[self.disabled_component][0]

    @property
    def method_variant(self) -> str:
        if not self.disabled_component:
            return "full"
        return _VARIANTS[self.disabled_component][1]

    @property
    def run_suffix(self) -> str:
        if not self.disabled_component:
            return ""
        return _VARIANTS[self.disabled_component][2]

    @property
    def compute_patch_coverage(self) -> bool:
        return not self.is_ablation

    @property
    def signature(self) -> str:
        return ";".join(
            f"{name}={int(bool(getattr(self, name)))}" for name in _COMPONENTS
        )

    def to_dict(self) -> dict[str, Any]:
        flags = {name: bool(getattr(self, name)) for name in _COMPONENTS}
        return {
            **flags,
            "ablation_id": self.ablation_id,
            "method_variant": self.method_variant,
            "disabled_component": self.disabled_component,
            "is_ablation": self.is_ablation,
            "run_suffix": self.run_suffix,
            "compute_patch_coverage": self.compute_patch_coverage,
            "signature": self.signature,
            "effective_feedback_routes": {
                "mode": (
                    "specialized" if self.specialized_feedback else "generic"
                ),
                "environment": bool(
                    self.specialized_feedback and self.environment_feedback
                ),
                "trigger": bool(
                    self.specialized_feedback and self.trigger_feedback
                ),
                "assertion": bool(
                    self.specialized_feedback and self.assertion_feedback
                ),
                "generic": not self.specialized_feedback,
            },
        }


def _remove_mutation_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _remove_mutation_fields(item)
            for key, item in value.items()
            if str(key).lower() not in {"mutation_hints", "mutation_plan"}
        }
    if isinstance(value, list):
        return [_remove_mutation_fields(item) for item in value]
    return copy.deepcopy(value)


def behavior_prompt_payload(behavior: Any, config: AblationConfig) -> dict[str, Any]:
    """Return prompt evidence without mutating the shared BehaviorTarget cache."""

    payload = behavior.to_dict()
    if config.mutation:
        return payload
    return _remove_mutation_fields(payload)


def render_ablation_prompt(
    prompt: str,
    config: AblationConfig,
    include_banner: bool = True,
) -> str:
    """Remove explicit mutation guidance only for the mutation ablation."""

    if config.mutation:
        return prompt
    rendered = re.sub(r"mutation[_ -]?hints", "trigger evidence", prompt, flags=re.I)
    rendered = re.sub(r"mutation[_ -]?plan", "trigger guidance", rendered, flags=re.I)
    rendered = re.sub(r"\bmutation\b", "adjustment", rendered, flags=re.I)
    rendered = rendered.replace("小变异", "必要调整").replace("变异", "调整")
    if not include_banner:
        return rendered
    return (
        "【实验条件：不使用显式调整规划】\n"
        "不提供显式调整提示或规划；仅依据 Issue、检索证据和执行反馈生成或修复测试。\n\n"
        + rendered
    )


def ablation_signature_from_summary(summary: dict[str, Any]) -> str:
    """Read a current signature or infer the two legacy full/BT variants."""

    direct = str(summary.get("ablation_signature") or "")
    if direct:
        return direct
    nested = summary.get("ablation_config")
    if isinstance(nested, dict) and nested.get("signature"):
        return str(nested["signature"])
    return AblationConfig(
        behavior_target=bool(summary.get("behavior_target_enabled", True))
    ).signature
