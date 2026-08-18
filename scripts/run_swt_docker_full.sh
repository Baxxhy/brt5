#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
SWT_ROOT=${SWT_ROOT:-$PACKAGE_ROOT/swt-bench}
OFFICIAL_ROOT=${OFFICIAL_ROOT:-$PACKAGE_ROOT/swt-bench-official-e0a1be1}
CONTROLLER_IMAGE=${CONTROLLER_IMAGE:-brt6-controller:py311}
MODEL=${MODEL:-DeepSeek-V4-Flash}
# A canonical full run starts at IssueRewrite.  The frozen 47.46 cache remains
# available for explicit ablations/reproduction, but silently using it here
# would make a run labelled "from scratch" start halfway through the method.
BEHAVIOR_TARGET_CACHE=${BEHAVIOR_TARGET_CACHE:-}
EXECUTION_TIMEOUT=${EXECUTION_TIMEOUT:-3000}
RUN_ITERATIVE_FEEDBACK=${RUN_ITERATIVE_FEEDBACK:-1}
FEEDBACK_ROUNDS=${FEEDBACK_ROUNDS:-3}
FEEDBACK_WORKERS=${FEEDBACK_WORKERS:-12}
REPAIR_WORKERS=${REPAIR_WORKERS:-10}
PORTFOLIO_ENABLED=${PORTFOLIO_ENABLED:-1}
SURROGATE_WORKERS=${SURROGATE_WORKERS:-6}
if [[ "${BRT6_IN_DOCKER_CONTROLLER:-0}" != "1" ]]; then
  # Prepare the compatibility envs while the Docker CLI is available on the
  # host.  Later stages run inside the controller and intentionally use only
  # the Docker SDK/socket.
  SWT_ENV_COMPAT=1 bash "$PROJECT_ROOT/scripts/prepare_matplotlib_compat_envs.sh"
  if ! docker image inspect "$CONTROLLER_IMAGE" >/dev/null 2>&1; then
    docker build -t "$CONTROLLER_IMAGE" -f "$PROJECT_ROOT/docker/controller.Dockerfile" "$PROJECT_ROOT/docker"
  fi
  mkdir -p "$HOME/.cache/huggingface"
  exec docker run --rm \
    --name "brt6-controller-$(date +%s)" \
    --network host \
    -e HTTP_PROXY -e HTTPS_PROXY -e ALL_PROXY -e NO_PROXY \
    -e http_proxy -e https_proxy -e all_proxy -e no_proxy \
    -e BRT6_IN_DOCKER_CONTROLLER=1 \
    -e RUN_TIMESTAMP="${RUN_TIMESTAMP:-}" \
    -e RUN_NAME="${RUN_NAME:-}" \
    -e RUN_DIR="${RUN_DIR:-}" \
    -e ISSUE_WORKERS="${ISSUE_WORKERS:-6}" \
    -e GENERATION_WORKERS="${GENERATION_WORKERS:-6}" \
    -e EVALUATION_WORKERS="${EVALUATION_WORKERS:-10}" \
    -e MODEL -e BEHAVIOR_TARGET_CACHE -e EXECUTION_TIMEOUT \
    -e RUN_ITERATIVE_FEEDBACK -e FEEDBACK_ROUNDS -e FEEDBACK_WORKERS -e REPAIR_WORKERS \
    -e PORTFOLIO_ENABLED -e SURROGATE_WORKERS \
    -e OFFICIAL_ROOT \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$PACKAGE_ROOT:$PACKAGE_ROOT" \
    -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
    -w "$PROJECT_ROOT" \
    "$CONTROLLER_IMAGE" \
    bash "$PROJECT_ROOT/scripts/run_swt_docker_full.sh"
fi

PYTHON_BIN=${PYTHON_BIN:-python3}
ISSUE_WORKERS=${ISSUE_WORKERS:-6}
GENERATION_WORKERS=${GENERATION_WORKERS:-6}
EVALUATION_WORKERS=${EVALUATION_WORKERS:-10}
INSTANCES_PATH=${INSTANCES_PATH:-$PROJECT_ROOT/data/issues/swt276_issues.json}
CODE_RETRIEVAL=${CODE_RETRIEVAL:-$PROJECT_ROOT/retrieval_results/code/code_retrieval_results_gpt.json}
TEST_RETRIEVAL=${TEST_RETRIEVAL:-$PROJECT_ROOT/retrieval_results/test/icore/gpt/related_tests.json}
REPO_ROOT=${REPO_ROOT:-$PACKAGE_ROOT/swe_repos}
RUN_TIMESTAMP=${RUN_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}
RUN_NAME=${RUN_NAME:-brt6_swt276_deepseek_v4_flash_$RUN_TIMESTAMP}
RUN_DIR=${RUN_DIR:-$PROJECT_ROOT/results/runs/$RUN_NAME}
SWT_RUN_ID=${SWT_RUN_ID:-$RUN_NAME}

mkdir -p "$RUN_DIR"/{issue_rewrite,generation,evaluation,logs,tmp}
export TMPDIR=$RUN_DIR/tmp
export BRT_API_POOL_FILE=${BRT_API_POOL_FILE:-$PROJECT_ROOT/.secrets/api_pool.json}
if [[ -n "$BEHAVIOR_TARGET_CACHE" ]]; then
  FROM_SCRATCH_ISSUE_REWRITE=false
else
  FROM_SCRATCH_ISSUE_REWRITE=true
fi

if ! "$PYTHON_BIN" -c 'import docker; assert docker.from_env(timeout=60).ping()' >/dev/null 2>&1; then
  echo "Docker daemon is unavailable" >&2
  exit 2
fi
if [[ ! -d "$SWT_ROOT/src" ]]; then
  echo "SWT-Bench checkout is missing: $SWT_ROOT" >&2
  exit 2
fi

cat > "$RUN_DIR/run_config.json" <<EOF
{
  "run_name": "$RUN_NAME",
  "model": "$MODEL",
  "runtime_backend": "swt_bench_docker",
  "host_conda_used": false,
  "dataset_size": 276,
  "instances_path": "$INSTANCES_PATH",
  "swt_root": "$SWT_ROOT",
  "issue_workers": $ISSUE_WORKERS,
  "generation_workers": $GENERATION_WORKERS,
  "evaluation_workers": $EVALUATION_WORKERS
  ,"behavior_target_cache": "$BEHAVIOR_TARGET_CACHE"
  ,"from_scratch_issue_rewrite": $FROM_SCRATCH_ISSUE_REWRITE
  ,"execution_timeout": $EXECUTION_TIMEOUT
  ,"iterative_feedback_rounds": $FEEDBACK_ROUNDS
}
EOF

cd "$PACKAGE_ROOT"
behavior_args=()
if [[ -n "$BEHAVIOR_TARGET_CACHE" ]]; then
  behavior_args+=(--behavior-target-cache "$BEHAVIOR_TARGET_CACHE")
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/validate_behavior_target_cache.py" \
    --cache-dir "$BEHAVIOR_TARGET_CACHE" \
    --instances-path "$INSTANCES_PATH" \
    --dataset-mode swt \
    --code-retrieval-path "$CODE_RETRIEVAL" \
    --test-retrieval-path "$TEST_RETRIEVAL" \
    2>&1 | tee "$RUN_DIR/logs/behavior_target_cache_validation.log"
  echo "Using frozen 47.46 F2P BehaviorTarget evidence; test generation is fresh." \
    | tee "$RUN_DIR/logs/issue_rewrite.log"
else
  for attempt in 1 2 3; do
    resume=()
    [[ "$attempt" -gt 1 ]] && resume+=(--resume)
    "$PYTHON_BIN" -m brt6.pipeline.run_issue_rewrite \
      --instances_path "$INSTANCES_PATH" \
      --code_retrieval_path "$CODE_RETRIEVAL" \
      --test_retrieval_path "$TEST_RETRIEVAL" \
      --output_dir "$RUN_DIR/issue_rewrite" \
      --llm-provider deepseek --model "$MODEL" \
      --max_workers "$ISSUE_WORKERS" --temperature 0.1 --max_tokens 4096 \
      "${resume[@]}" 2>&1 | tee -a "$RUN_DIR/logs/issue_rewrite.log"
    count=$(find "$RUN_DIR/issue_rewrite" -mindepth 2 -maxdepth 2 -name behavior_target.json | wc -l)
    [[ "$count" -eq 276 ]] && break
  done
  if [[ "${count:-0}" -ne 276 ]]; then
    echo "Issue rewrite incomplete after retries: ${count:-0}/276; recording missing instances and continuing" \
      | tee -a "$RUN_DIR/logs/issue_rewrite.log"
  fi
fi

for attempt in 1 2 3; do
  resume=()
  [[ "$attempt" -gt 1 ]] && resume+=(--resume)
  generation_command=("$PYTHON_BIN" -m brt6.pipeline.run \
    --instances_path "$INSTANCES_PATH" \
    --code_retrieval_path "$CODE_RETRIEVAL" \
    --test_retrieval_path "$TEST_RETRIEVAL" \
    --repo_root_base "$REPO_ROOT" \
    --output_dir "$RUN_DIR/generation" \
    --llm-provider deepseek --model "$MODEL" \
    --max_workers "$GENERATION_WORKERS" \
    --temperature 0.1 --max_tokens 4096 \
    --runtime_backend swt_bench_docker --no_conda --generate_only \
    --timeout "$EXECUTION_TIMEOUT" \
    "${behavior_args[@]}" \
    "${resume[@]}")
  if [[ -n "$BEHAVIOR_TARGET_CACHE" ]]; then
    "${generation_command[@]}" 2>&1 | tee -a "$RUN_DIR/logs/generation.log"
  else
    BRT4_BEHAVIOR_CACHE_DIR="$RUN_DIR/issue_rewrite" \
      "${generation_command[@]}" 2>&1 | tee -a "$RUN_DIR/logs/generation.log"
  fi
  count=$(find "$RUN_DIR/generation" -mindepth 2 -maxdepth 2 -name final_test.py | wc -l)
  [[ "$count" -eq 276 ]] && break
done
[[ "${count:-0}" -gt 0 ]] || { echo "No tests were generated" >&2; exit 4; }
if [[ "$count" -ne 276 ]]; then
  echo "Generation produced $count/276 tests; empty predictions will count as failures in official evaluation" \
    | tee -a "$RUN_DIR/logs/generation.log"
fi
touch "$RUN_DIR/generation.done"

DIRECT_GENERATION=
if [[ "$PORTFOLIO_ENABLED" == "1" ]]; then
  DIRECT_GENERATION=$RUN_DIR/generation_direct
  mkdir -p "$DIRECT_GENERATION"
  for attempt in 1 2 3; do
    resume=()
    [[ "$attempt" -gt 1 ]] && resume+=(--resume)
    direct_command=("$PYTHON_BIN" -m brt6.pipeline.run \
      --instances_path "$INSTANCES_PATH" \
      --code_retrieval_path "$CODE_RETRIEVAL" \
      --test_retrieval_path "$TEST_RETRIEVAL" \
      --repo_root_base "$REPO_ROOT" --output_dir "$DIRECT_GENERATION" \
      --llm-provider deepseek --model "$MODEL" --max_workers "$GENERATION_WORKERS" \
      --temperature 0.1 --max_tokens 4096 --runtime_backend swt_bench_docker \
      --no_conda --generate_only --mutation false --timeout "$EXECUTION_TIMEOUT" \
      "${behavior_args[@]}" "${resume[@]}")
    if [[ -n "$BEHAVIOR_TARGET_CACHE" ]]; then
      "${direct_command[@]}" 2>&1 | tee -a "$RUN_DIR/logs/generation_direct.log"
    else
      BRT4_BEHAVIOR_CACHE_DIR="$RUN_DIR/issue_rewrite" \
        "${direct_command[@]}" 2>&1 | tee -a "$RUN_DIR/logs/generation_direct.log"
    fi
    direct_count=$(find "$DIRECT_GENERATION" -mindepth 2 -maxdepth 2 -name final_test.py | wc -l)
    [[ "$direct_count" -eq 276 ]] && break
  done
  if [[ "${direct_count:-0}" -ne 276 ]]; then
    echo "Direct portfolio generation incomplete: ${direct_count:-0}/276" >&2
    exit 4
  fi
  touch "$RUN_DIR/generation_direct.done"
fi

if [[ "$RUN_ITERATIVE_FEEDBACK" == "1" ]]; then
  # This launcher is already running inside brt6-controller.  Enter the
  # iterative script's controller branch directly; otherwise it tries to use
  # the Docker CLI from inside the controller image even though execution is
  # provided through the mounted daemon socket and Python Docker SDK.
  BRT6_IN_ITERATIVE_CONTROLLER=1 BRT6_IN_OFFICIAL_CONTROLLER=1 \
  SOURCE_RUN="$RUN_DIR" RUN_NAME="$RUN_NAME" RUN_DIR="$RUN_DIR" \
  MODEL="$MODEL" OFFICIAL_ROOT="$OFFICIAL_ROOT" \
  FEEDBACK_ROUNDS="$FEEDBACK_ROUNDS" FEEDBACK_WORKERS="$FEEDBACK_WORKERS" \
  REPAIR_WORKERS="$REPAIR_WORKERS" EVALUATION_WORKERS="$EVALUATION_WORKERS" \
  EXECUTION_TIMEOUT="$EXECUTION_TIMEOUT" \
  DIRECT_GENERATION="$DIRECT_GENERATION" PORTFOLIO_ENABLED="$PORTFOLIO_ENABLED" \
  SURROGATE_WORKERS="$SURROGATE_WORKERS" \
    bash "$PROJECT_ROOT/scripts/run_swt_iterative_docker_full.sh"
  exit $?
fi

PREDICTIONS=$RUN_DIR/evaluation/predictions.jsonl
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/export_swt_predictions.py" \
  --instances "$INSTANCES_PATH" \
  --generation-dir "$RUN_DIR/generation" \
  --output "$PREDICTIONS" \
  --model-name brt6__DeepSeek-V4-Flash \
  | tee "$RUN_DIR/logs/export_swt_predictions.log"

RUN_NAME="$RUN_NAME" RUN_DIR="$RUN_DIR" SWT_RUN_ID="$SWT_RUN_ID" \
SWT_ENV_COMPAT=1 PREDICTIONS="$PREDICTIONS" \
EVALUATION_WORKERS="$EVALUATION_WORKERS" EVALUATION_TIMEOUT="$EXECUTION_TIMEOUT" \
  bash "$PROJECT_ROOT/scripts/run_swt_official_clean_full.sh"
