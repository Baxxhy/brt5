#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}
PACKAGE_ROOT=${PACKAGE_ROOT:-$(cd "$PROJECT_ROOT/.." && pwd)}

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi

usage() {
  cat <<'EOF'
Usage: bash scripts/run_p0_simple_llm_selector_full.sh --dataset {swt|tdd} [--behavior-target {on|off}]

Options:
  --dataset {swt|tdd}  Select the experiment dataset (default: swt).
  --behavior-target {on|off}
                       Enable BehaviorTarget (default: on). Use off for the
                       "w/o Behavior Target" ablation; IssueRewrite is skipped.
  -h, --help           Show this help message.

DATASET_MODE, INSTANCES_PATH, GOLD_DATASET, and RUN_DIR may still be
overridden through environment variables.
EOF
}

DATASET_MODE=${DATASET_MODE:-swt}
ENABLE_BEHAVIOR_TARGET=${ENABLE_BEHAVIOR_TARGET:-true}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset)
      if [[ $# -lt 2 ]]; then
        echo "--dataset requires swt or tdd" >&2
        usage >&2
        exit 2
      fi
      DATASET_MODE=$2
      shift 2
      ;;
    --dataset=*)
      DATASET_MODE=${1#*=}
      shift
      ;;
    --behavior-target)
      if [[ $# -lt 2 ]]; then
        echo "--behavior-target requires on or off" >&2
        usage >&2
        exit 2
      fi
      case "$2" in
        on|true|1) ENABLE_BEHAVIOR_TARGET=true ;;
        off|false|0) ENABLE_BEHAVIOR_TARGET=false ;;
        *)
          echo "invalid --behavior-target value '$2': expected on or off" >&2
          exit 2
          ;;
      esac
      shift 2
      ;;
    --behavior-target=*)
      BEHAVIOR_TARGET_VALUE=${1#*=}
      case "$BEHAVIOR_TARGET_VALUE" in
        on|true|1) ENABLE_BEHAVIOR_TARGET=true ;;
        off|false|0) ENABLE_BEHAVIOR_TARGET=false ;;
        *)
          echo "invalid --behavior-target value '$BEHAVIOR_TARGET_VALUE': expected on or off" >&2
          exit 2
          ;;
      esac
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$ENABLE_BEHAVIOR_TARGET" in
  true|1|on) ENABLE_BEHAVIOR_TARGET=true ;;
  false|0|off) ENABLE_BEHAVIOR_TARGET=false ;;
  *)
    echo "invalid ENABLE_BEHAVIOR_TARGET='$ENABLE_BEHAVIOR_TARGET': expected true or false" >&2
    exit 2
    ;;
esac

CONDA_EXE=${CONDA_EXE:-}
if [[ -z "$CONDA_EXE" ]] && command -v conda >/dev/null 2>&1; then
  CONDA_EXE=$(command -v conda)
fi
for CANDIDATE in \
  "$HOME/miniforge3/bin/conda" \
  "$HOME/miniconda3/bin/conda" \
  "/root/conda/ENTER/bin/conda"; do
  if [[ -z "$CONDA_EXE" && -x "$CANDIDATE" ]]; then
    CONDA_EXE=$CANDIDATE
  fi
done
if [[ -z "$CONDA_EXE" || ! -x "$CONDA_EXE" ]]; then
  echo "Conda is unavailable. Run: bash scripts/bootstrap_machine.sh" >&2
  exit 2
fi
CONDA_BASE=$("$CONDA_EXE" info --base)
export CONDA_EXE
export BRT3_CONDA_SH=${BRT3_CONDA_SH:-$CONDA_BASE/etc/profile.d/conda.sh}
export BRT_WORKSPACE_ROOT=${BRT_WORKSPACE_ROOT:-$PACKAGE_ROOT}
export PATH="$(dirname "$CONDA_EXE"):$PATH"
export PYTHONPATH="$PACKAGE_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON_BIN=${PYTHON_BIN:-$CONDA_BASE/envs/icore/bin/python}
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Framework environment 'icore' is unavailable. Run: bash scripts/bootstrap_machine.sh" >&2
  exit 2
fi

case "$DATASET_MODE" in
  swt)
    DEFAULT_INSTANCES_PATH=$PROJECT_ROOT/data/issues/swt276_issues.json
    ;;
  tdd)
    DEFAULT_INSTANCES_PATH=$PACKAGE_ROOT/TDD-Bench-Verified/TDD_Bench.json
    ;;
  *)
    echo "invalid dataset '$DATASET_MODE': expected swt or tdd" >&2
    exit 2
    ;;
esac

RUN_TIMESTAMP=${RUN_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}
RUN_VARIANT_SUFFIX=""
if [ "$ENABLE_BEHAVIOR_TARGET" = "false" ]; then
  RUN_VARIANT_SUFFIX="_wo_behavior_target"
fi
RUN_DIR=${RUN_DIR:-$PROJECT_ROOT/results/runs/p0_simple_llm_selector_${DATASET_MODE}${RUN_VARIANT_SUFFIX}_${RUN_TIMESTAMP}}
INSTANCES_PATH=${INSTANCES_PATH:-$DEFAULT_INSTANCES_PATH}
GOLD_DATASET=${GOLD_DATASET:-$INSTANCES_PATH}
CODE_RETRIEVAL=${CODE_RETRIEVAL:-$PROJECT_ROOT/retrieval_results/code/code_retrieval_results_gpt.json}
TEST_RETRIEVAL=${TEST_RETRIEVAL:-$PROJECT_ROOT/retrieval_results/test/icore/gpt/related_tests.json}
REPO_ROOT=${REPO_ROOT:-$PACKAGE_ROOT/swe_repos}
MODEL=${MODEL:-deepseek-v3}
ISSUE_WORKERS=${ISSUE_WORKERS:-6}
GENERATION_WORKERS=${GENERATION_WORKERS:-6}
EVALUATION_WORKERS=${EVALUATION_WORKERS:-6}
RUNTIME_BACKEND=${RUNTIME_BACKEND:-local_conda}

API_POOL_SIZE=$("$PYTHON_BIN" - <<'PY'
from brt5.llm.api_pool import configured_apis
print(len(configured_apis()))
PY
)
if [[ "$API_POOL_SIZE" -eq 0 ]]; then
  echo "No API key is configured. Run: $PYTHON_BIN $PROJECT_ROOT/scripts/configure_api_keys.py" >&2
  exit 3
fi
echo "api_pool_entries=$API_POOL_SIZE"

DATASET_SIZE=$("$PYTHON_BIN" - "$INSTANCES_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
if isinstance(data, list):
    rows = [row for row in data if isinstance(row, dict)]
elif isinstance(data, dict):
    rows = [row for row in data.values() if isinstance(row, dict)]
else:
    raise SystemExit(f"unsupported dataset structure in {path}")
instance_ids = [str(row.get("instance_id") or "") for row in rows]
if not rows or any(not instance_id for instance_id in instance_ids):
    raise SystemExit(f"dataset contains empty or invalid rows: {path}")
if len(instance_ids) != len(set(instance_ids)):
    raise SystemExit(f"dataset contains duplicate instance_id values: {path}")
print(len(rows))
PY
) || exit $?

ISSUE_DIR=$RUN_DIR/issue_rewrite
GENERATION_DIR=$RUN_DIR/generation
FORMAL_DIR=$RUN_DIR/evaluation/formal_f2p
LOG_DIR=$RUN_DIR/logs
mkdir -p "$ISSUE_DIR" "$GENERATION_DIR" "$FORMAL_DIR" "$LOG_DIR" "$RUN_DIR/tmp"

exec > >(tee -a "$LOG_DIR/full_pipeline.log") 2>&1

TDD_PREFLIGHT_PATH=""
TDD_RUN_LOCK_PATH=""
if [ "$DATASET_MODE" = "tdd" ]; then
  if [ "$RUNTIME_BACKEND" != "local_conda" ]; then
    echo "TDD only supports RUNTIME_BACKEND=local_conda; Docker is intentionally disabled" >&2
    exit 2
  fi
  TDD_RUN_LOCK_PATH=${TDD_RUN_LOCK_PATH:-$HOME/.cache/brt5/_locks/tdd_local_conda_full.lock}
  mkdir -p "$(dirname "$TDD_RUN_LOCK_PATH")"
  exec {TDD_RUN_LOCK_FD}>"$TDD_RUN_LOCK_PATH"
  if ! flock -n "$TDD_RUN_LOCK_FD"; then
    echo "another full TDD local-Conda run holds $TDD_RUN_LOCK_PATH" >&2
    exit 73
  fi
  export BRT_TDD_STRICT_LOCAL_CONDA=1
  TDD_PREFLIGHT_PATH=$RUN_DIR/tdd_local_conda_preflight.json
fi

echo "__BRT_STAGE__ start $(date --iso-8601=seconds)"
echo "dataset_mode=$DATASET_MODE"
echo "instances_path=$INSTANCES_PATH"
echo "dataset_size=$DATASET_SIZE"
echo "behavior_target_enabled=$ENABLE_BEHAVIOR_TARGET"
if [ "$ENABLE_BEHAVIOR_TARGET" = "true" ]; then
  echo "method_variant=full"
else
  echo "method_variant=w/o Behavior Target"
fi
echo "framework_python=$PYTHON_BIN"
if [ "$DATASET_MODE" = "tdd" ]; then
  echo "runtime_backend=$RUNTIME_BACKEND"
  echo "docker_harness_invoked=false"
  echo "strict_runtime_scrub=$BRT_TDD_STRICT_LOCAL_CONDA"
  echo "tdd_run_lock=$TDD_RUN_LOCK_PATH"
  echo "__BRT_STAGE__ tdd_local_conda_preflight_start $(date --iso-8601=seconds)"
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/preflight_tdd_local_conda.py" \
    --instances_path "$INSTANCES_PATH" \
    --repo_root_base "$REPO_ROOT" \
    --output_path "$TDD_PREFLIGHT_PATH"
  TDD_PREFLIGHT_RC=$?
  echo "__BRT_STAGE__ tdd_local_conda_preflight_end rc=$TDD_PREFLIGHT_RC $(date --iso-8601=seconds)"
  if [ "$TDD_PREFLIGHT_RC" -ne 0 ]; then
    echo "TDD local-Conda preflight failed; see $TDD_PREFLIGHT_PATH" >&2
    exit "$TDD_PREFLIGHT_RC"
  fi
fi
"$PYTHON_BIN" -c 'import sys; print("resolved_framework_python=" + sys.executable)'

cd "$PACKAGE_ROOT"

ISSUE_RC=1
ISSUE_TARGETS=0
if [ "$ENABLE_BEHAVIOR_TARGET" = "true" ]; then
  echo "__BRT_STAGE__ issue_rewrite_start $(date --iso-8601=seconds)"
  for ISSUE_ATTEMPT in 1 2 3; do
    RESUME_ARGS=()
    if [ "$ISSUE_ATTEMPT" -gt 1 ]; then
      RESUME_ARGS+=(--resume)
    fi
    echo "__BRT_STAGE__ issue_rewrite_attempt=$ISSUE_ATTEMPT"
    "$PYTHON_BIN" -m brt5.pipeline.run_issue_rewrite \
      --instances_path "$INSTANCES_PATH" \
      --code_retrieval_path "$CODE_RETRIEVAL" \
      --test_retrieval_path "$TEST_RETRIEVAL" \
      --output_dir "$ISSUE_DIR" \
      --model "$MODEL" \
      --max_workers "$ISSUE_WORKERS" \
      --temperature 0.1 \
      --max_tokens 4096 \
      "${RESUME_ARGS[@]}"
    ISSUE_RC=$?
    ISSUE_TARGETS=$("$PYTHON_BIN" - "$INSTANCES_PATH" "$ISSUE_DIR" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
rows = data if isinstance(data, list) else list(data.values())
issue_dir = Path(sys.argv[2])
print(sum((issue_dir / str(row.get("instance_id")) / "behavior_target.json").is_file() for row in rows))
PY
)
    echo "__BRT_PROGRESS__ issue_targets=$ISSUE_TARGETS/$DATASET_SIZE rc=$ISSUE_RC"
    if [ "$ISSUE_RC" -eq 0 ] && [ "$ISSUE_TARGETS" -eq "$DATASET_SIZE" ]; then
      break
    fi
    sleep 5
  done
  echo "__BRT_STAGE__ issue_rewrite_end rc=$ISSUE_RC $(date --iso-8601=seconds)"

  if [ "$ISSUE_RC" -ne 0 ] || [ "$ISSUE_TARGETS" -eq 0 ]; then
    echo "__BRT_STAGE__ abort_no_behavior_targets rc=$ISSUE_RC targets=$ISSUE_TARGETS"
    exit "$ISSUE_RC"
  fi
else
  ISSUE_RC=0
  echo "__BRT_STAGE__ issue_rewrite_skipped reason=w/o_behavior_target $(date --iso-8601=seconds)"
fi

echo "__BRT_STAGE__ generation_start $(date --iso-8601=seconds)"
GENERATION_RUNTIME_ARGS=()
if [ "$DATASET_MODE" = "tdd" ]; then
  GENERATION_RUNTIME_ARGS+=(--dataset_mode tdd --runtime_backend local_conda)
fi
GENERATION_COMMAND=(
"$PYTHON_BIN" -m brt5.pipeline.run \
  --instances_path "$INSTANCES_PATH" \
  --code_retrieval_path "$CODE_RETRIEVAL" \
  --test_retrieval_path "$TEST_RETRIEVAL" \
  --repo_root_base "$REPO_ROOT" \
  --output_dir "$GENERATION_DIR" \
  --model "$MODEL" \
  --max_workers "$GENERATION_WORKERS" \
  --max_feedback_rounds 3 \
  --max_env_rounds 2 \
  --max_brt_rounds 3 \
  --validation_mode buggy_only \
  --timeout 1800 \
  --temperature 0.1 \
  --max_tokens 4096 \
  --enable_behavior_target "$ENABLE_BEHAVIOR_TARGET" \
  "${GENERATION_RUNTIME_ARGS[@]}"
)
if [ "$ENABLE_BEHAVIOR_TARGET" = "true" ]; then
  BRT4_BEHAVIOR_CACHE_DIR="$ISSUE_DIR" "${GENERATION_COMMAND[@]}"
else
  env -u BRT4_BEHAVIOR_CACHE_DIR "${GENERATION_COMMAND[@]}"
fi
GENERATION_RC=$?
echo "__BRT_STAGE__ generation_end rc=$GENERATION_RC $(date --iso-8601=seconds)"

echo "__BRT_STAGE__ formal_f2p_start $(date --iso-8601=seconds)"
FORMAL_RUNTIME_ARGS=()
if [ "$DATASET_MODE" = "tdd" ]; then
  FORMAL_RUNTIME_ARGS+=(--dataset_mode tdd --runtime_backend local_conda)
fi
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/run_formal_eval_after_generation.py" \
  --outputs_dir "$GENERATION_DIR" \
  --dataset_file "$GOLD_DATASET" \
  --repo_root_base "$REPO_ROOT" \
  --max_workers "$EVALUATION_WORKERS" \
  --timeout 1800 \
  --evaluation_dir "$FORMAL_DIR" \
  --log_path "$LOG_DIR/formal_eval.log" \
  --summary_path "$RUN_DIR/evaluation/formal_eval_summary.json" \
  --compute_patch_coverage true \
  "${FORMAL_RUNTIME_ARGS[@]}"
EVALUATION_RC=$?
echo "__BRT_STAGE__ formal_f2p_end rc=$EVALUATION_RC $(date --iso-8601=seconds)"

"$PYTHON_BIN" - "$RUN_DIR" "$INSTANCES_PATH" "$DATASET_MODE" "$ENABLE_BEHAVIOR_TARGET" "$ISSUE_RC" "$GENERATION_RC" "$EVALUATION_RC" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

run = Path(sys.argv[1])
instances_path = Path(sys.argv[2])
dataset_mode = sys.argv[3]
behavior_target_enabled = sys.argv[4].lower() == 'true'
issue_rc, generation_rc, evaluation_rc = map(int, sys.argv[5:])
data = json.loads(instances_path.read_text(encoding="utf-8"))
instances = data if isinstance(data, list) else list(data.values())
generated = sum(
    (run / 'generation' / row['instance_id'] / 'final_test.py').is_file()
    for row in instances
)
metrics_path = run / 'evaluation' / 'formal_f2p' / 'metrics.json'
metrics = json.loads(metrics_path.read_text()) if metrics_path.is_file() else {}
completion = {
    'finished_at': datetime.now(timezone.utc).astimezone().isoformat(),
    'dataset_mode': dataset_mode,
    'behavior_target_enabled': behavior_target_enabled,
    'method_variant': 'full' if behavior_target_enabled else 'w/o Behavior Target',
    'instances_path': str(instances_path),
    'issue_rewrite_returncode': issue_rc,
    'generation_returncode': generation_rc,
    'evaluation_returncode': evaluation_rc,
    'dataset_size': len(instances),
    'generated_tests': generated,
    'missing_generation': len(instances) - generated,
    'formal_total_instances': metrics.get('total_instances'),
    'f2p_success': metrics.get('f2p_success'),
    'f2p_fail': metrics.get('f2p_fail'),
    'f2p_at_1_percent': metrics.get('f2p_at_1_percent'),
    'by_status': metrics.get('by_status', {}),
    'denominator_valid': metrics.get('total_instances') == len(instances),
    'patch_cov_enabled': metrics.get('patch_cov_enabled'),
    'patch_cov_at_1_percent': metrics.get('patch_cov_at_1_percent'),
    'patch_cov_delta_at_1_percent': metrics.get('patch_cov_delta_at_1_percent'),
    'patch_cov_definition': metrics.get('patch_cov_definition'),
}
(run / 'completion.json').write_text(json.dumps(completion, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(completion, ensure_ascii=False, indent=2))
PY

echo "__BRT_STAGE__ complete $(date --iso-8601=seconds)"
exit "$EVALUATION_RC"
