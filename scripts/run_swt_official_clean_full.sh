#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
OFFICIAL_ROOT=${OFFICIAL_ROOT:-$PACKAGE_ROOT/swt-bench-official-e0a1be1}
CONTROLLER_IMAGE=${CONTROLLER_IMAGE:-brt6-controller:py311}
OFFICIAL_COMMIT=${OFFICIAL_COMMIT:-e0a1be1}
RUN_NAME=${RUN_NAME:-brt6_swt276_deepseek_v4_flash_official_e0a1be1_$(date +%Y%m%d_%H%M%S)}
RUN_DIR=${RUN_DIR:-$PROJECT_ROOT/results/runs/$RUN_NAME}
SWT_RUN_ID=${SWT_RUN_ID:-$RUN_NAME}
EVALUATION_WORKERS=${EVALUATION_WORKERS:-4}
EVALUATION_TIMEOUT=${EVALUATION_TIMEOUT:-3000}
SWT_ENV_COMPAT=${SWT_ENV_COMPAT:-0}
PREDICTIONS=${PREDICTIONS:-$PROJECT_ROOT/results/runs/brt6_swt276_deepseek_v4_flash_20260810_035010/evaluation/predictions.jsonl}
INSTANCE_IDS_FILE=${INSTANCE_IDS_FILE:-}
DOCKER_CONFIG_DIR=$PROJECT_ROOT/docker/official-docker-config
RELAY_LOG=$RUN_DIR/logs/docker_proxy_relay.log
TMPDIR=${TMPDIR:-$RUN_DIR/tmp}
mkdir -p "$TMPDIR"
export TMPDIR

if [[ "${BRT6_IN_OFFICIAL_CONTROLLER:-0}" != "1" ]]; then
  mkdir -p "$RUN_DIR"/{evaluation,logs,official_workspace}
  relay_pid=
  if ! timeout 2 bash -c '</dev/tcp/172.18.0.1/17890' 2>/dev/null; then
    /usr/bin/python3 "$PROJECT_ROOT/scripts/docker_proxy_relay.py" \
      --listen-host 172.18.0.1 --listen-port 17890 \
      --target-host 127.0.0.1 --target-port 7890 \
      >>"$RELAY_LOG" 2>&1 &
    relay_pid=$!
    sleep 1
    kill -0 "$relay_pid"
  fi
  cleanup_relay() {
    if [[ -n "$relay_pid" ]]; then
      kill "$relay_pid" 2>/dev/null || true
      wait "$relay_pid" 2>/dev/null || true
    fi
  }
  trap cleanup_relay EXIT INT TERM

  if [[ "$SWT_ENV_COMPAT" == "1" ]]; then
    bash "$PROJECT_ROOT/scripts/prepare_matplotlib_compat_envs.sh"
  fi

  if ! docker image inspect "$CONTROLLER_IMAGE" >/dev/null 2>&1; then
    docker build -t "$CONTROLLER_IMAGE" -f "$PROJECT_ROOT/docker/controller.Dockerfile" "$PROJECT_ROOT/docker"
  fi
  docker run --rm \
    --name "brt6-official-${SWT_RUN_ID}-$(date +%s)" \
    --network host \
    -e HTTP_PROXY -e HTTPS_PROXY -e ALL_PROXY -e NO_PROXY \
    -e http_proxy -e https_proxy -e all_proxy -e no_proxy \
    -e TMPDIR \
    -e BRT6_IN_OFFICIAL_CONTROLLER=1 \
    -e OFFICIAL_ROOT -e OFFICIAL_COMMIT -e RUN_NAME -e RUN_DIR \
    -e SWT_RUN_ID -e EVALUATION_WORKERS -e EVALUATION_TIMEOUT \
    -e PREDICTIONS -e INSTANCE_IDS_FILE -e SWT_ENV_COMPAT \
    -e DOCKER_CONFIG=/root/.docker \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$PACKAGE_ROOT:$PACKAGE_ROOT" \
    -v "$OFFICIAL_ROOT:$OFFICIAL_ROOT:ro" \
    -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
    -v "$DOCKER_CONFIG_DIR:/root/.docker:ro" \
    -w "$RUN_DIR/official_workspace" \
    "$CONTROLLER_IMAGE" \
    bash "$PROJECT_ROOT/scripts/run_swt_official_clean_full.sh"
  exit $?
fi

[[ -f "$PREDICTIONS" ]] || { echo "Missing predictions: $PREDICTIONS" >&2; exit 2; }
[[ "$(git -C "$OFFICIAL_ROOT" rev-parse HEAD)" == "$OFFICIAL_COMMIT"* ]] || {
  echo "Official checkout is not pinned to $OFFICIAL_COMMIT" >&2
  exit 2
}
[[ -z "$(git -C "$OFFICIAL_ROOT" status --porcelain --untracked-files=no)" ]] || {
  echo "Official checkout has tracked modifications" >&2
  exit 2
}

export PYTHONPATH="$PROJECT_ROOT/scripts/official_runtime:$OFFICIAL_ROOT:$OFFICIAL_ROOT/src"
python3 -c 'import docker; assert docker.from_env(timeout=60).ping()'

PREDICTIONS_TO_EVAL=$PREDICTIONS
if [[ -n "$INSTANCE_IDS_FILE" ]]; then
  PREDICTIONS_TO_EVAL=$RUN_DIR/evaluation/predictions_selected.jsonl
  python3 "$PROJECT_ROOT/scripts/filter_swt_predictions.py" \
    --predictions "$PREDICTIONS" \
    --instance-ids "$INSTANCE_IDS_FILE" \
    --output "$PREDICTIONS_TO_EVAL"
fi

# The iterative launcher can enter this script directly from an existing
# controller, bypassing the outer branch that normally creates this directory.
# Create it unconditionally before installing the official dataset symlink.
mkdir -p "$RUN_DIR/official_workspace"
if [[ ! -e "$RUN_DIR/official_workspace/dataset" ]]; then
  ln -s "$OFFICIAL_ROOT/dataset" "$RUN_DIR/official_workspace/dataset"
fi
cd "$RUN_DIR/official_workspace"
SWT_MAIN=(python3 -m src.main)
if [[ "$SWT_ENV_COMPAT" == "1" ]]; then
  SWT_MAIN=(python3 "$PROJECT_ROOT/scripts/run_swt_official_env_compat.py")
fi
"${SWT_MAIN[@]}" \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path "$PREDICTIONS_TO_EVAL" \
  --filter_swt \
  --max_workers "$EVALUATION_WORKERS" \
  --run_id "$SWT_RUN_ID" \
  --cache_level instance --clean false \
  --build_mode api --timeout "$EVALUATION_TIMEOUT" \
  2>&1 | tee "$RUN_DIR/logs/official_swt_evaluation.log"

python3 "$PROJECT_ROOT/scripts/summarize_swt_f2p.py" \
  --swt-root "$RUN_DIR/official_workspace" \
  --run-id "$SWT_RUN_ID" \
  --model-name brt6__DeepSeek-V4-Flash \
  --predictions "$PREDICTIONS_TO_EVAL" \
  --output "$RUN_DIR/evaluation/f2p_summary.json" \
  | tee "$RUN_DIR/logs/f2p_summary.log"

# Keep one canonical combined metrics artifact containing both F2P and the
# official SWT-Bench Patch Coverage fields.  The definitions are unchanged;
# this is the same parsed report, exposed under the conventional metrics name.
cp "$RUN_DIR/evaluation/f2p_summary.json" "$RUN_DIR/evaluation/metrics.json"

touch "$RUN_DIR/evaluation.done" "$RUN_DIR/done"
