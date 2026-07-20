# Validated Mutation Planning Pilot

## Contract

- Baseline commit: `9daaa983ea335c6b1889922bcb82cd9a726b9296`.
- Dataset: SWT-Bench Lite, using the frozen `full_method_f2p_47_46_20260717` BehaviorTarget cache.
- Primary metric: formal F2P on a bounded canary; no full 276 run in this pass.
- Scope: Full Method (`mutation=on`) only. Do not change the `w/o Mutation` prompt path.

## Implementation

1. Make mutation steps structured and trigger-only.
2. Parse string and object operations; never invent `CALL_CHAIN_EXTEND`.
3. Validate paths, symbols, seed anchors, protocol preservation, risk, and oracle isolation.
4. Gracefully fall back to direct generation when planning is invalid or unavailable.
5. Allow at most one feedback trigger replan per seed.
6. Record validation/adherence metadata without letting plan existence outrank runtime evidence.

## Verification

- Python compilation and focused unit tests.
- Full unit test suite when focused tests pass.
- Executed SWT canary: `sympy__sympy-11400`, `pytest-dev__pytest-8906`,
  `django__django-12497`. The remaining proposed instances are reserved for a
  larger pre-full-run pilot.
- Acceptance: no forced mutation default, no high-risk plan reaches generation, planner failure degrades safely, at most one trigger replan, and formal F2P artifacts have no environment failure.
