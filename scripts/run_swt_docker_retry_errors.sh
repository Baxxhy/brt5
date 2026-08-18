#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
SWT_ROOT=${SWT_ROOT:-$PACKAGE_ROOT/swt-bench}
CONTROLLER_IMAGE=${CONTROLLER_IMAGE:-brt6-controller:py311}
RUN_NAME=${RUN_NAME:-brt6_swt276_deepseek_v4_flash_20260810_035010}
RUN_DIR=${RUN_DIR:-$PROJECT_ROOT/results/runs/$RUN_NAME}
SWT_RUN_ID=${SWT_RUN_ID:-$RUN_NAME}
EVALUATION_WORKERS=${EVALUATION_WORKERS:-3}
MODEL_NAME=${MODEL_NAME:-brt6__DeepSeek-V4-Flash}

if [[ "${BRT6_IN_DOCKER_CONTROLLER:-0}" != "1" ]]; then
  docker build -t "$CONTROLLER_IMAGE" -f "$PROJECT_ROOT/docker/controller.Dockerfile" "$PROJECT_ROOT/docker"
  exec docker run --rm \
    --name "brt6-retry-${SWT_RUN_ID}-$(date +%s)" \
    --network host \
    -e HTTP_PROXY -e HTTPS_PROXY -e ALL_PROXY -e NO_PROXY \
    -e http_proxy -e https_proxy -e all_proxy -e no_proxy \
    -e BRT6_IN_DOCKER_CONTROLLER=1 \
    -e RUN_NAME -e RUN_DIR -e SWT_RUN_ID -e EVALUATION_WORKERS -e MODEL_NAME \
    -e SWT_DOCKER_BUILD_TIMEOUT="${SWT_DOCKER_BUILD_TIMEOUT:-7200}" \
    -e SWT_PIP_EDITABLE_NO_BUILD_ISOLATION="${SWT_PIP_EDITABLE_NO_BUILD_ISOLATION:-1}" \
    -e SWT_PIP_DROP_NO_USE_PEP517="${SWT_PIP_DROP_NO_USE_PEP517:-1}" \
    -e SWT_PIP_INSTALL_RETRIES="${SWT_PIP_INSTALL_RETRIES:-3}" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$PACKAGE_ROOT:$PACKAGE_ROOT" \
    -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
    -w "$PROJECT_ROOT" \
    "$CONTROLLER_IMAGE" \
    bash "$PROJECT_ROOT/scripts/run_swt_docker_retry_errors.sh"
fi

PYTHON_BIN=${PYTHON_BIN:-python3}
PREDICTIONS=$RUN_DIR/evaluation/predictions.jsonl
[[ -f "$PREDICTIONS" ]] || { echo "Missing predictions: $PREDICTIONS" >&2; exit 2; }
"$PYTHON_BIN" -c 'import docker; assert docker.from_env(timeout=60).ping()' >/dev/null

mkdir -p "$RUN_DIR/evaluation" "$RUN_DIR/logs"
cd "$SWT_ROOT"
echo "===== retry started $(date -Is), Docker build network=host, pip compatibility enabled =====" \
  | tee -a "$RUN_DIR/logs/swt_docker_retry_errors.log"
"$PYTHON_BIN" -m src.main \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path "$PREDICTIONS" \
  --filter_swt \
  --max_workers "$EVALUATION_WORKERS" \
  --run_id "$SWT_RUN_ID" \
  --cache_level env --clean true \
  --build_mode api --timeout 1800 \
  2>&1 | tee -a "$RUN_DIR/logs/swt_docker_retry_errors.log"

"$PYTHON_BIN" "$PROJECT_ROOT/scripts/summarize_swt_f2p.py" \
  --swt-root "$SWT_ROOT" \
  --run-id "$SWT_RUN_ID" \
  --model-name "$MODEL_NAME" \
  --predictions "$PREDICTIONS" \
  --output "$RUN_DIR/evaluation/f2p_summary.json" \
  | tee "$RUN_DIR/logs/f2p_summary.log"

touch "$RUN_DIR/evaluation.retry.done"
