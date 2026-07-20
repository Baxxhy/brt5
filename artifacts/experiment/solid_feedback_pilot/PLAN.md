# Solid Feedback Pilot Plan

## Selected idea

Keep the validated trigger-only Mutation Plan, but close its remaining grounding,
adherence, ranking, and reporting gaps. Replace hard single-label semantic repair
routing with a full-evidence repair core guided by setup/trigger/oracle focus
constraints. Oracle feedback covers all public observations, not only `assert`.

## Non-negotiable constraints

- Work only on `codex/validated-mutation-planning`.
- Preserve the frozen SWT BehaviorTarget cache from the 47.46% full-method run.
- Do not use surrogate or golden-patch information during generation.
- Do not alter formal F2P or Patch Coverage evaluation code.
- Do not launch the full 276-instance experiment in this pilot.
- Keep environment preparation and top-3 iCoRe seed exploration unchanged.

## Baseline and comparison contract

- Code baseline: current validated-mutation-planning worktree before this feedback pass.
- BehaviorTarget: `data/behavior_targets/swt/full_method_f2p_47_46_20260717`.
- Dataset: bounded SWT canary selected from prior failure modes.
- Primary acceptance metric: formal F2P with the canary size as the fixed denominator.
- Engineering acceptance: compile checks and all unit tests pass.

## Minimal code-change map

- `validation/mutation_plan_validator.py`: require source/seed-grounded symbols and
  exact AST-grounded anchors/before states.
- `validation/mutation_adherence.py`: AST-aware application checks and generalized
  oracle fingerprinting.
- `execution/feedback.py`: normalize semantic focus, use a full-evidence unified
  repair path, hard-reject mutation violations, and aggregate all seed attempts.
- `generation/generator.py` and prompts: pass complete evidence and protect all
  oracle forms (exception, warning, logging, state, return/output), not only asserts.
- tests: add focused regression coverage for every changed contract.

## Pilot path

1. Run Python compile and focused tests.
2. Run the complete unit-test suite.
3. Run a small SWT canary from frozen BehaviorTargets.
4. Run formal F2P only for that fixed canary.
5. Inspect per-seed plans, feedback routes, oracle contracts, and denominator.

## Stop conditions

- Stop before full 276 execution.
- Do not accept a pilot with environment/setup failure.
- Do not claim full-method improvement from the bounded canary.
- If a change breaks the last known 3/3 Mutation canary, diagnose before adding
  any further mechanism.

## Expected outputs

- Updated source and regression tests in the current worktree.
- Pilot generation/evaluation under `results/runs/`.
- Durable commands and findings in `CHECKLIST.md` and final handoff.

## Revision log

- 2026-07-20: initial contract created before feedback implementation.
