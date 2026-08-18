#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUN_NAME=${1:?usage: launch_failure_directed_v1_full.sh RUN_NAME}
SOURCE_RUN=${SOURCE_RUN:-$PROJECT_ROOT/results/runs/brt6_swt276_deepseek_v4_flash_ccfa_portfolio_full_20260817_030000}

export RUN_NAME
export RUN_DIR=${RUN_DIR:-$PROJECT_ROOT/results/runs/$RUN_NAME}
export SOURCE_RUN
export DIRECT_GENERATION=${DIRECT_GENERATION:-$SOURCE_RUN/generation_direct}
export FEEDBACK_ROUNDS=3
export FEEDBACK_WORKERS=${FEEDBACK_WORKERS:-12}
export REPAIR_WORKERS=${REPAIR_WORKERS:-10}
export EVALUATION_WORKERS=${EVALUATION_WORKERS:-12}
export SURROGATE_WORKERS=${SURROGATE_WORKERS:-6}
export EXECUTION_TIMEOUT=${EXECUTION_TIMEOUT:-3000}
export MODEL=${MODEL:-DeepSeek-V4-Flash}
export ADAPTIVE_ENABLED=1
export PORTFOLIO_ENABLED=1
unset INSTANCE_IDS_FILE || true

exec bash "$PROJECT_ROOT/scripts/run_swt_iterative_docker_full.sh"
