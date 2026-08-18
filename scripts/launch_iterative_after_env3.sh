#!/usr/bin/env bash
set -euo pipefail

ENV3_PID=${ENV3_PID:?ENV3_PID is required}
ENV3_RUN_DIR=${ENV3_RUN_DIR:?ENV3_RUN_DIR is required}
RUN_NAME=${RUN_NAME:?RUN_NAME is required}
RUN_DIR=${RUN_DIR:?RUN_DIR is required}

tail --pid="$ENV3_PID" -f /dev/null
python3 - "$ENV3_RUN_DIR/evaluation/f2p_summary.json" <<'PY'
import json
import sys
summary = json.load(open(sys.argv[1]))
if summary.get("total_instances") != 3 or summary.get("error_instances") != 0:
    raise SystemExit(f"env3 prerequisite failed: {summary}")
print("env3 prerequisite passed: 3/3 official reports")
PY

exec env \
  RUN_NAME="$RUN_NAME" RUN_DIR="$RUN_DIR" \
  FEEDBACK_ROUNDS="${FEEDBACK_ROUNDS:-3}" \
  FEEDBACK_WORKERS="${FEEDBACK_WORKERS:-12}" \
  REPAIR_WORKERS="${REPAIR_WORKERS:-10}" \
  EVALUATION_WORKERS="${EVALUATION_WORKERS:-12}" \
  BRT_SWE_REPOS="${BRT_SWE_REPOS:-/root/Baxxhy/BugReproduce/swe_repos}" \
  bash /root/Baxxhy/BugReproduce/brt6/scripts/run_swt_iterative_docker_full.sh
