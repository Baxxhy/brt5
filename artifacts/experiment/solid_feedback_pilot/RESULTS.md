# Solid Feedback Pilot Results

## Fixed inputs

- Branch: `codex/validated-mutation-planning`
- Dataset family: SWT-Bench Lite
- BehaviorTarget cache source:
  `data/behavior_targets/swt/full_method_f2p_47_46_20260717`
- Cache provenance: `131/276 = 47.4638%`, DeepSeek-V3, temperature 0.1
- Generation environment: `icore`
- Formal evaluation: isolated local Conda clones, F2P only

## Validation

- `python -m compileall -q core context execution generation mutation validation tests`
- `PYTHONPATH=/root/Baxxhy/BugReproduce /root/conda/ENTER/envs/icore/bin/python -m unittest discover -s tests -v`
- Result: 171 tests passed.

## Five-instance canary

Run directory:
`results/runs/solid_feedback_swt5_20260720`

Instances:

1. `django__django-12497`
2. `pytest-dev__pytest-8906`
3. `sympy__sympy-11400`
4. `django__django-13768`
5. `matplotlib__matplotlib-23987`

Result: generated 5/5, formal F2P 5/5, fixed denominator 5,
`patch_cov_enabled=false`, `invalid_environment=false`.

This bounded result validates execution and the selected regression paths. It
does not estimate the full 276-instance F2P.

## Non-assert Oracle regression

The first warning canary revealed that a Trigger Mutation Plan could edit
`pytest.warns`/`warnings.catch_warnings` because plan protection recognized
mainly assert/raises syntax. The validator now rejects warning, logging,
matcher, and snapshot edits as Oracle edits. The rerun selected a one-step
Trigger-only plan with `FULL` adherence and formal F2P success for
`matplotlib__matplotlib-23987`.

The first logging rerun exposed a fixed-side-only protocol failure:
`self.assertTestIsClean()` existed in the seed's original class but had not been
recovered into the new test module. ProtocolRecovery now recursively extracts
only class-local helpers actually referenced by the seed. The final run
contains the helper definition and no `AttributeError`.

Final logging run directory:
`results/runs/solid_feedback_logging_swt1_v2_20260720`

Result: formal F2P 1/1, `patch_cov_enabled=false`, no environment failure. Its
three seeds made three Mutation Plan calls; a non-adherent plan candidate was
persisted and explicitly downgraded to a direct no-plan fallback. The selected
checkpoint had hard eligibility 1, generalized Oracle kinds
`FRAMEWORK_ASSERTION + LOGGING`, and no Oracle-preservation violation.

## Remaining limitation

LLM generation remains stochastic. A canary success is an engineering gate,
not evidence that F2P improved on the complete benchmark. The next scientifically
valid comparison is a paired 276-instance full-method run against Mutation-off
using this exact code revision and the same frozen BehaviorTarget cache.
