# Checklist

- [x] Preserve and push baseline commit.
- [x] Create isolated implementation branch.
- [x] Implement structured trigger-only mutation schema.
- [x] Implement parser, validator, and safe fallback.
- [x] Integrate generator and feedback budget.
- [x] Add focused unit tests.
- [x] Run compile and unit tests (159 passed).
- [x] Run bounded SWT canary with frozen BehaviorTarget.
- [x] Record canary commands and results.

## Final canary

- Run: `results/runs/validated_mutation_final_swt3_20260720`.
- Instances: `django__django-12497`, `pytest-dev__pytest-8906`,
  `sympy__sympy-11400`.
- Generation: 3/3 `ISSUE_ALIGNED_FAIL`; no environment failure.
- Formal F2P: 3/3 (`100%`), denominator 3.
- Patch Coverage: disabled for this bounded validation.
- Every selected candidate: one `VALID` low-risk plan and `FULL` adherence.
