#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=/root/Baxxhy/BugReproduce/brt6
PACKAGE_ROOT=/root/Baxxhy/BugReproduce
OFFICIAL_ROOT=/root/Baxxhy/BugReproduce/swt-bench
RUN_DIR=${RUN_DIR:-$PROJECT_ROOT/results/runs/brt6_swt276_deepseek_v4_flash_ccfa_portfolio_full_20260817_030000}
OUTPUT_DIR=${OUTPUT_DIR:-$RUN_DIR/evaluation/golden_oracle_audit}
INSTANCES=${INSTANCES:-$PROJECT_ROOT/data/issues/swt276_issues.json}
AUDIT_RUN_ID=${AUDIT_RUN_ID:-brt6-golden-oracle-audit-20260818}
MAX_WORKERS=${MAX_WORKERS:-12}
TIMEOUT=${TIMEOUT:-3000}
CONTROLLER_IMAGE=${CONTROLLER_IMAGE:-brt6-controller:py311}
MODE_ARGS=("$@")

mkdir -p "$OUTPUT_DIR" "$RUN_DIR/logs"

if [[ "${BRT6_IN_GOLDEN_AUDIT_CONTROLLER:-0}" != "1" ]]; then
  exec docker run --rm \
    --name "brt6-golden-oracle-audit-$(date +%s)" \
    --network host \
    -e HTTP_PROXY -e HTTPS_PROXY -e ALL_PROXY -e NO_PROXY \
    -e http_proxy -e https_proxy -e all_proxy -e no_proxy \
    -e BRT6_IN_GOLDEN_AUDIT_CONTROLLER=1 \
    -e RUN_DIR -e OUTPUT_DIR -e INSTANCES -e AUDIT_RUN_ID \
    -e MAX_WORKERS -e TIMEOUT -e CONTROLLER_IMAGE \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$PACKAGE_ROOT:$PACKAGE_ROOT" \
    -v /root/.cache/huggingface:/root/.cache/huggingface \
    -w "$PROJECT_ROOT" \
    "$CONTROLLER_IMAGE" \
    bash "$PROJECT_ROOT/scripts/run_golden_oracle_audit.sh" "${MODE_ARGS[@]}"
fi

export PYTHONPATH="$PROJECT_ROOT/scripts:$PROJECT_ROOT/scripts/official_runtime:$OFFICIAL_ROOT:$OFFICIAL_ROOT/src:$PACKAGE_ROOT"
python3 "$PROJECT_ROOT/scripts/golden_oracle_audit.py" \
  --run-dir "$RUN_DIR" \
  --instances "$INSTANCES" \
  --output-dir "$OUTPUT_DIR" \
  --run-id "$AUDIT_RUN_ID" \
  --max-workers "$MAX_WORKERS" \
  --timeout "$TIMEOUT" \
  "${MODE_ARGS[@]}"
