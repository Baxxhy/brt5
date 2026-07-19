# P0 Simple LLM Selector Checklist

## Identity

- branch: `codex/run-p0-simple-llm-selector`
- pilot run: `p0_simple_llm_selector_canary5_20260717`
- full run: `p0_simple_llm_selector_full276_20260717`
- stage: eight-instance minimal-environment canary complete

## Code freeze

- [x] HEAD confirmed as `p0-136-f2p-49.28` / `a167ad6`
- [x] previous broad refactor preserved in stash
- [x] dynamic reachability removed from active flow
- [x] independent oracle-risk selection removed
- [x] LLM-only semantic selector implemented
- [x] simple ranking implemented
- [x] surrogate calls remain absent
- [x] environment and formal-evaluation fixes preserved
- [x] focused tests pass under literal `icore` (7 method + 50 environment tests)

## Five-instance pilot

- [x] fresh IssueRewrite 5/5
- [x] fresh generation 5/5
- [x] zero surrogate calls verified
- [x] project commands use recorded `setup_*` environments
- [x] formal F2P evaluates denominator 5
- [x] no environment/setup/collection failures
- [x] required metric keys complete and finite
- [x] at least one `F2P_SUCCESS` (3/5 = 60.0%)

## Full 276

- [x] dataset with official gold patches prepared for evaluation only
- [x] full detached chain launched under literal `icore`
- [x] generation configured for all 276 rows
- [x] evaluation configured for all 276 rows including missing generations
- [x] project evaluation resolves each instance's own environment
- [x] command, PID, and durable logs recorded

## Environment recovery

- [x] original full run completed: 273 generated, 3 missing, 131/276 F2P
- [x] missing instances fixed to the exact three `MISSING_GENERATION` rows
- [x] failed hashed environments confirmed present and recoverable
- [x] three isolated recovery generations launched under literal `icore` (`brt5_env3_recovery_f2p`)
- [x] all three recovered `final_test.py` files verified in the eight-instance recovery run
- [x] zero surrogate calls verified for all three recovered summaries
- [x] eight-row generation view is complete (8/8); unchanged 268 rows remain in the frozen source result
- [x] eight formal rows are merged into the frozen 276-row result with denominator 276
- [x] recovery result recorded: 132/276 F2P, delta +1 versus 131/276

## Environment isolation code repair

- [x] requirements files are worktree-local and unique per environment
- [x] legacy `$HOME/requirements.txt` scripts are rewritten before execution
- [x] dependency templates are never used for project setup or tests
- [x] generation uses a fresh instance runtime clone and records its template
- [x] formal evaluation uses a separate fresh instance runtime clone
- [x] runtime clones are removed after their owning instance
- [x] Astropy 1.3 dependency contract matches its build/runtime requirements
- [x] Pylint 2.15 formal evaluation preserves the contract Astroid version
- [x] environment manifest fingerprints and `pip check` results are recorded
- [x] regression tests for the above are written
- [x] static syntax and 67 focused unit tests executed under literal iCoRe
- [x] three missing-instance generations executed under literal iCoRe
- [x] all eight instances formally evaluated in isolated project environments (6 success, 2 fixed fail, 0 environment errors)
- [x] eight rows merged into the 276-row result with denominator 276 (132/276 = 47.8261%)
- [x] no `brt5i_*` runtime environments or generation/evaluation processes remain

## B-machine environment-integrity repair

- [x] B-machine failure mechanisms mapped to concrete environment code paths
- [x] duplicate dependency metadata is rejected
- [x] imported dependency versions and ABI-sensitive imports are checked
- [x] repair purges orphan metadata and force-reinstalls coherent dependency groups
- [x] disposable clones scrub stale benchmark-project namespace and editable residue
- [x] legacy Matplotlib setuptools protocol is compatible with warnings-as-errors startup
- [x] all 127 unit tests pass under literal `icore`; all 105 Python files compile
- [x] real Xarray, Scikit-learn, and current Matplotlib template integrity probes pass
- [x] contaminated legacy Matplotlib template is rejected; current hashed template passes after targeted repair
- [x] diff reviewed and release commit prepared for the B-machine rerun
