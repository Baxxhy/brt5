# Frozen SWT BehaviorTarget cache

This directory contains the exact 276 `behavior_target.json` files produced by
the IssueRewrite stage of:

`p0_simple_llm_selector_swt_generic_iteration_20260720_025533`

The source model was `deepseek-v3` with temperature `0.1`. The cache uses the
lossless `behavior_target.lossless.v1` schema. `manifest.json` binds these files
to the SWT-276 dataset and the exact code/test retrieval inputs with SHA-256
hashes. It also records one hash for every BehaviorTarget.

Validate it from the repository root:

```bash
conda run -n icore python scripts/validate_behavior_target_cache.py \
  --cache-dir data/behavior_targets/swt/generic_iteration_20260720_025533 \
  --instances-path data/issues/swt276_issues.json \
  --dataset-mode swt \
  --code-retrieval-path retrieval_results/code/code_retrieval_results_gpt.json \
  --test-retrieval-path retrieval_results/test/icore/gpt/related_tests.json
```

Use it in a full or single-factor ablation run:

```bash
bash scripts/run_p0_simple_llm_selector_full.sh \
  --dataset swt \
  --behavior-target-cache data/behavior_targets/swt/generic_iteration_20260720_025533
```

Do not edit individual targets or the manifest. Any change is rejected before
generation. Omitting `--behavior-target-cache` intentionally creates a new
BehaviorTarget version through IssueRewrite.
