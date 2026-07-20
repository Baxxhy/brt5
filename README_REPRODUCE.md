# Fresh-machine reproduction

The repository can prepare a new Debian/Ubuntu Linux machine, but API credentials are never
stored in Git. A first full run downloads Miniforge, framework dependencies,
the benchmark repositories, and per-project Conda
environments. Expect substantial network traffic, disk use, and setup time.

## 1. Publish a clean repository

The original Git history contained a tracked multi-key pool. Deleting the file
in a later commit does not delete it from history. Revoke/rotate those keys and
publish a fresh history:

```bash
python scripts/export_clean_repo.py /tmp/brt5-public
cd /tmp/brt5-public
git status
git commit -m "reproducible BRT experiment"
git remote add origin <YOUR_NEW_REPOSITORY_URL>
git push -u origin main
```

Do not push the current historical repository until the old keys have been
revoked or its history has been independently purged and verified.

## 2. Bootstrap a new Linux machine

```bash
git clone <YOUR_NEW_REPOSITORY_URL> brt5
cd brt5
bash scripts/bootstrap_machine.sh --dataset swt
```

The bootstrap does the following:

1. installs Linux build prerequisites with `apt-get`;
2. installs Miniforge when Conda is absent;
3. creates the `icore` framework environment;
4. installs the project-only framework dependencies from `requirements.txt`;
5. prompts for one or more DeepSeek-compatible keys and stores them at
   `.secrets/api_pool.json` with mode `0600`;
6. clones all repositories required by the selected dataset;
7. verifies every `base_commit` and `environment_setup_commit`.

For both SWT and TDD inputs:

```bash
bash scripts/bootstrap_machine.sh --dataset all
```

`--dataset all` also clones
`https://github.com/IBM/TDD-Bench-Verified.git` beside the BRT repository.

## 3. Configure keys without committing them

Interactive multi-key configuration:

```bash
~/miniforge3/envs/icore/bin/python scripts/configure_api_keys.py
```

Alternatively copy `config/api_pool.example.json` to
`.secrets/api_pool.json`, replace the placeholder locally, and run:

```bash
chmod 600 .secrets/api_pool.json
```

Environment variables are also supported:

```bash
export DEEPSEEK_API_KEYS='key1,key2,key3'
export DEEPSEEK_BASE_URL='https://api.deepseek.com'
export DEEPSEEK_MODEL='deepseek-v3'
```

For a remote machine or cluster, pass these values through its secret manager,
or transfer `.secrets/api_pool.json` separately over an authenticated channel.
Do not put that file in the repository, even when the repository is private.

## 4. Run the full and ablation experiments

The repository contains the frozen 276-instance BehaviorTarget version from
the full-method run `p0_simple_llm_selector_full276_20260717`, whose formal
F2P result was `131/276 = 47.4638%`:

```bash
BEHAVIOR_CACHE=data/behavior_targets/swt/full_method_f2p_47_46_20260717

# Optional independent integrity check before a run.
conda run -n icore python scripts/validate_behavior_target_cache.py \
  --cache-dir "$BEHAVIOR_CACHE" \
  --instances-path data/issues/swt276_issues.json \
  --dataset-mode swt \
  --code-retrieval-path retrieval_results/code/code_retrieval_results_gpt.json \
  --test-retrieval-path retrieval_results/test/icore/gpt/related_tests.json
```

Pass `--behavior-target-cache "$BEHAVIOR_CACHE"` to reuse this exact version.
The launcher validates all 276 targets and their dataset/retrieval hashes, then
skips IssueRewrite. If the option is omitted, the launcher performs
IssueRewrite and generates a new BehaviorTarget version for that run.

```bash
# Full B*=<Environment, Trigger, Assertion>
bash scripts/run_p0_simple_llm_selector_full.sh \
  --dataset swt --behavior-target on \
  --behavior-target-cache "$BEHAVIOR_CACHE"

# w/o Behavior Target
bash scripts/run_p0_simple_llm_selector_full.sh \
  --dataset swt --behavior-target off

# w/o Mutation
bash scripts/run_p0_simple_llm_selector_full.sh \
  --dataset swt --mutation off \
  --behavior-target-cache "$BEHAVIOR_CACHE"

# Generic Iteration (w/o specialized feedback)
bash scripts/run_p0_simple_llm_selector_full.sh \
  --dataset swt --specialized-feedback off \
  --behavior-target-cache "$BEHAVIOR_CACHE"

# w/o Environment Feedback
bash scripts/run_p0_simple_llm_selector_full.sh \
  --dataset swt --environment-feedback off \
  --behavior-target-cache "$BEHAVIOR_CACHE"

# w/o Trigger Feedback
bash scripts/run_p0_simple_llm_selector_full.sh \
  --dataset swt --trigger-feedback off \
  --behavior-target-cache "$BEHAVIOR_CACHE"

# w/o Assertion Feedback
bash scripts/run_p0_simple_llm_selector_full.sh \
  --dataset swt --assertion-feedback off \
  --behavior-target-cache "$BEHAVIOR_CACHE"
```

At most one component may be `off` in a command. Every ablation runs formal
F2P only and keeps the complete dataset denominator; the all-on full method
also runs the benchmark-specific coverage metric.

`w/o Behavior Target` is the only experiment that must not receive
`--behavior-target-cache`, because consuming the cache would invalidate that
ablation. The launcher rejects that combination.

The launcher uses the repository location dynamically. Generation runs in the
`icore` framework environment; every benchmark project runs in its own
iCoRe-derived Conda environment. Missing generated tests remain in the formal
evaluation denominator. Formal output includes F2P and Change Coverage (Delta C).

To install only the BRT5 framework environment without preparing any SWT/TDD
repository or per-instance environment:

```bash
conda create -n icore -y python=3.12 pip
conda run -n icore python -m pip install -r requirements.txt
```

This installs the framework and its transitive Python dependencies only. The
benchmark-project environments are prepared separately by iCoRe when an
experiment actually needs them.

## Limits of “one command”

The machine still needs Debian/Ubuntu Linux, network access, enough disk space, and either
root/sudo for automatic OS-package installation or equivalent build tools
installed beforehand. Secrets cannot be reconstructed from a Git clone; they
must be supplied locally or through a CI secret store.
