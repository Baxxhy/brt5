# Joint-Seed Mutation Ablation Checklist

- [x] Fix the one-factor comparison contract.
- [x] Audit current Top-3 routing and prompt rewriting.
- [x] Add ordered Top-3 reference bundle.
- [x] Add positive joint-seed generation prompt.
- [x] Remove negative mutation-ablation prompt disclosure.
- [x] Route Mutation-off through one candidate pipeline.
- [x] Preserve the full-method per-seed path unchanged.
- [x] Add regression tests for routing, prompt, planner count, and metadata.
- [x] Pass compile and full unit tests.
- [x] Record final behavior and limitations.

## Verification record

- Python compile check: passed.
- Shell syntax and `git diff --check`: passed.
- Focused ablation tests: 20/20 passed.
- Full unit suite: 174/174 passed with a test-process-only `CONDA_EXE=/bin/true`
  override because the host's real `/root/conda/ENTER/bin/conda --version`
  currently times out after 30 seconds.
- Prompt-family audit: no alternate-method or disabled-component disclosure in
  the joint generation, protocol, verifier, or observation instructions.
- Full SWT generation/evaluation: intentionally not started in this change.
