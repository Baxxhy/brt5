# Joint-Seed Mutation Ablation Plan

## Selected change

Redefine only `w/o Mutation`: iCoRe Top-3 retrieved tests are supplied together
in one generation request, producing one candidate that continues through the
existing protocol, execution, specialized feedback, verifier, ranking, and F2P
pipeline. The full method remains the existing per-seed validated-Mutation flow.

## Non-negotiable controls

- Triggered only by `--mutation off`.
- Preserve iCoRe retrieval order and use rank 0 as the runner/placement/protocol
  anchor.
- Supply rank 0-2 code, file, name, and rank to one generation request.
- Keep the same BehaviorTarget evidence except downstream `mutation_hints`.
- Keep environment, feedback, verifier, repair budgets, and formal F2P unchanged.
- Call Mutation Planner zero times and emit no Mutation Plan artifacts.
- Use a dedicated positive joint-seed prompt. Do not disclose disabled or
  alternative mechanisms to the model.
- Do not change the full-method prompt or full-method Top-3 execution path.

## Code map

- `core/schema.py`: persist the ordered reference-seed bundle in HostContext.
- `core/ablation.py`: remove negative/global prompt rewriting.
- `core/prompts.py` and `prompts/joint_seed_generation/`: positive joint prompt.
- `execution/feedback.py`: one joint pipeline for Mutation-off; preserve the
  existing full-method adaptive Top-3 branch.
- `generation/generator.py`: select the joint prompt and carry the same bundle
  into feedback evidence.
- tests: prove one generation call, three ordered references, zero planner calls,
  no negative ablation disclosure, and unchanged full-method routing.

## Validation boundary

- Compile and full unit tests.
- Bounded mocked end-to-end routing test; no full SWT run in this change unless
  separately requested.

## Revision log

- 2026-07-20: contract fixed before implementation.
