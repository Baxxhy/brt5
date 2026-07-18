# P0 Simple LLM Selector Experiment Plan

## 1. Method contract

- Branch: `codex/run-p0-simple-llm-selector`
- Baseline source: frozen best tag `p0-136-f2p-49.28` at `a167ad6`.
- Immediate measured comparator: 116/276 F2P from `run_setup_trigger_oracle_full276_20260717_035400`.
- Primary objective: recover F2P while keeping the method exactly as agreed with the user.
- Method: lossless `setup/trigger/oracle` BehaviorTarget; iCoRe seed order; real buggy execution; execution-log feedback and repair; full-Issue LLM semantic validation; simple final ranking.
- Explicit exclusions: no surrogate patch generation, execution, validation, ranking, or early stop; no runtime target tracing/coverage; no receiver/MRO or API-boundary inference; no independent oracle-risk score in selection.

## 2. Evaluation contract

- Dataset: `data/issues/swt276_issues.json`, exactly 276 rows.
- Pilot: the fixed five previous failures in `results/runs/p0_lossless_nosurrogate_recovery_20260717/canary5.json`.
- Primary metric: formal gold-patch F2P@1 only.
- Missing generated tests remain in the denominator and count as failures.
- Framework commands must use `/root/conda/ENTER/envs/icore/bin/python`.
- Generated tests and formal buggy/fixed runs must use each project's configured `setup_*` iCoRe environment recorded in generation metadata.
- Generation never receives or reads the gold patch.

## 3. Minimal code change

1. Keep the P0 generation, protocol recovery, mutation, execution-feedback, and repair flow.
2. Keep the lossless three-part BehaviorTarget and original iCoRe ordering.
3. Remove dynamic reachability from executor, verifier, checkpoints, prompts, and ranking.
4. Remove independent oracle-risk scoring from checkpoints and final selection.
5. Keep the LLM verifier over full Issue, candidate, command, real stdout/stderr, BehaviorTarget, ProtocolRecovery, and source context.
6. Rank candidates lexicographically by semantic acceptance, issue alignment, grounded oracle, public behavior, original iCoRe seed rank, and fewer repair rounds.
7. Preserve current environment-manager and isolated formal-evaluator fixes.

## 4. Pilot gate

The five-instance pilot must satisfy all of the following before launching 276:

- IssueRewrite produces 5/5 lossless BehaviorTargets.
- Generation produces 5/5 `final_test.py` and 5/5 summaries.
- Every generation summary records zero surrogate calls and `buggy_only` validation.
- No generation or formal result is an environment/setup/collection error.
- Formal evaluation reports `total_instances=5`; missing generation, if any, is counted in that total.
- Required finite metrics exist: `total_instances`, `f2p_success`, `f2p_fail`, `f2p_at_1_percent`, and `by_status`.
- At least one of the five previous failures becomes `F2P_SUCCESS`; otherwise stop and diagnose rather than spend the 276 budget.

## 5. Full run

- Run ID: `p0_simple_llm_selector_full276_20260717`.
- IssueRewrite and generation use bounded concurrency under the literal `icore` interpreter.
- Formal evaluation starts automatically after generation and evaluates all 276 dataset rows, not only completed generations.
- Formal evaluation computes F2P only (`compute_patch_coverage=false`).
- The full chain is detached after the pilot gate; no continuing monitor is required.
- Durable commands, logs, manifests, environment policy, generation completeness, and formal metrics live under the run directory.

## 6. Stop and retry rules

- Do not change method code during the pilot.
- Retry only for a concrete implementation, environment, or evaluation failure.
- Do not launch 276 on incomplete pilot metrics, denominator drift, system-Python leakage, or zero pilot F2P.
- Once 276 is launched, do not tune code from partial results.

## 7. Revision log

| Date | Change | Reason |
|---|---|---|
| 2026-07-17 | Replaced dynamic-evidence selector design with buggy execution + LLM verifier + simple ranking | Match the user's intended method and keep the P0 recovery controlled |
| 2026-07-18 | Add an environment-only recovery run for the three missing generations, followed by a full 276-row F2P reevaluation | The main run completed at 131/276 with exactly three `MISSING_GENERATION` cases caused by recoverable Conda lifecycle failures |
| 2026-07-18 | Replace mutable shared project environments with immutable dependency templates plus per-instance runtime clones; isolate generated requirement files and unify dependency pins | The three-instance recovery exposed a cross-process `$HOME/requirements.txt` race and deterministic Astropy/Pylint contract drift |
| 2026-07-18 | Restore dependency pins after Conda clone and use the iCoRe project setup command in formal evaluation | Conda clone reintroduced setuptools 80.9 into Astropy runtimes, while the generic formal setup omitted Pylint runtime dependencies |
| 2026-07-18 | Complete the eight-instance canary and denominator-stable result merge | Final canary is 6/8 with zero environment errors; merged result is 132/276 (47.8261%), +1 success over the frozen 131/276 result |

## 8. Missing-generation recovery contract

- Recovery run ID: `p0_simple_llm_selector_env3_recovery_20260718`.
- Scope is limited to `astropy__astropy-6938`, `pylint-dev__pylint-5859`, and `pylint-dev__pylint-7080`.
- Reuse the 276-run IssueRewrite cache, retrieval inputs, generation parameters, framework interpreter, and method code.
- Exercise the existing deterministic recovery branch: remove each incompatible failed hashed environment and recreate it from the original iCoRe environment spec.
- Keep the original generation and formal evaluation directories read-only.
- Write each recovery instance into an isolated raw output directory and preserve its command log.
- Acceptance gate: all three recovery instance directories contain top-level `final_test.py` and `summary.json`, and all summaries report zero surrogate calls.
- On acceptance, create a symlink-only merged generation view containing the original 273 tests plus the three recovered tests, then run the same formal evaluator on all 276 rows with PatchCov disabled.
- If any target still lacks `final_test.py`, stop before formal reevaluation and diagnose that concrete environment recreation failure; do not broaden the retry.

## 9. Environment-isolation implementation contract

- This pass first changed code only; the user subsequently authorized an eight-instance canary run on 2026-07-18.
- Dependency-template environments are validated against the iCoRe dependency contract and are never used for project installation or test execution.
- Generation and formal evaluation each clone the validated template into a deterministic instance/workspace-specific runtime environment; project installs and dependency recovery are confined to that clone.
- Runtime clones are removed after their owning generation/evaluation instance completes, while metadata retains the immutable template identity needed by formal evaluation.
- Requirements heredocs use a worktree-local, environment-specific path; no environment script may read or write `$HOME/requirements.txt` or `/root/requirements.txt`.
- Astropy 1.3 declares `numpy==1.23.5` and `cython<3` in the dependency contract itself. Pylint 2.15 formal setup must not replace the contract's Astroid pin.
- Each generation/evaluation record captures a package-manifest fingerprint and `pip check` result for later reproducibility auditing.

## 10. Eight-instance minimal-environment canary

- Scope: `astropy__astropy-12907`, `astropy__astropy-14182`, `astropy__astropy-14995`, `astropy__astropy-6938`, `pylint-dev__pylint-7114`, `pylint-dev__pylint-7993`, `pylint-dev__pylint-5859`, and `pylint-dev__pylint-7080`.
- Reuse the five existing generated tests; regenerate only the three previously missing tests under the literal iCoRe interpreter.
- Evaluate all eight in clean per-instance runtime clones using their configured project environments.
- Override these eight rows in the completed 276-row formal result; missing generation remains a failure and the denominator remains exactly 276.
- Final run: `p0_minimal_envfix_canary8_20260718_104530`.
- Final canary: 6/8 F2P, 2 `FIXED_FAIL`, zero environment errors.
- Final merged result: 132/276 F2P (47.8261%), with status counts 132 `F2P_SUCCESS`, 125 `FIXED_FAIL`, and 19 `BUGGY_PASS`.
