# Solid Feedback Pilot Checklist

- [x] Fix branch and frozen BehaviorTarget comparison contract.
- [x] Record implementation and pilot boundaries.
- [x] Tighten Mutation Plan source/anchor grounding.
- [x] Replace substring-only adherence with AST-aware checks.
- [x] Treat plan violations as selection-ineligible.
- [x] Generalize Oracle feedback beyond `assert`.
- [x] Give semantic repair routes identical full evidence.
- [x] Normalize reject/failure-class routing.
- [x] Record all-seed and selected-seed call counts separately.
- [x] Add focused regression tests.
- [x] Pass compile and full unit tests (171 passed in `icore`).
- [x] Pass bounded frozen-BehaviorTarget SWT generation.
- [x] Pass formal F2P with a fixed denominator and no environment failure.
- [x] Audit artifacts and record limitations.

## End-to-end evidence

- Frozen cache source: SWT full-method run `131/276 = 47.4638%`.
- Five-instance canary: generated `5/5`; formal F2P `5/5`; Patch Coverage
  explicitly disabled; no environment failure.
- Non-assert regression exposed and fixed two real defects:
  warning/logging Oracle edits escaping Trigger-plan validation, and a referenced
  class helper omitted by ProtocolRecovery.
- Final logging regression (`django__django-13768`): generated `1/1`; formal F2P
  `1/1`; selected checkpoint hard-eligible; Oracle kinds
  `FRAMEWORK_ASSERTION + LOGGING`; Patch Coverage disabled.
- Full-dataset performance is intentionally not claimed from these canaries.
