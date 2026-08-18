#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
OFFICIAL_ROOT=${OFFICIAL_ROOT:-$PACKAGE_ROOT/swt-bench-official-e0a1be1}
CONTROLLER_IMAGE=${CONTROLLER_IMAGE:-brt6-controller:py311}
SOURCE_RUN=${SOURCE_RUN:-$PROJECT_ROOT/results/runs/brt6_swt276_deepseek_v4_flash_ccfa_portfolio_full_20260817_030000}
RUN_NAME=${RUN_NAME:-brt6_swt276_deepseek_v4_flash_docker_feedback_$(date +%Y%m%d_%H%M%S)}
RUN_DIR=${RUN_DIR:-$PROJECT_ROOT/results/runs/$RUN_NAME}
FEEDBACK_ROUNDS=${FEEDBACK_ROUNDS:-3}
FEEDBACK_WORKERS=${FEEDBACK_WORKERS:-12}
REPAIR_WORKERS=${REPAIR_WORKERS:-10}
EVALUATION_WORKERS=${EVALUATION_WORKERS:-12}
EXECUTION_TIMEOUT=${EXECUTION_TIMEOUT:-3000}
MODEL=${MODEL:-DeepSeek-V4-Flash}
INSTANCE_IDS_FILE=${INSTANCE_IDS_FILE:-}
DIRECT_GENERATION=${DIRECT_GENERATION:-$SOURCE_RUN/generation_direct}
PORTFOLIO_ENABLED=${PORTFOLIO_ENABLED:-1}
SURROGATE_WORKERS=${SURROGATE_WORKERS:-6}
ADAPTIVE_ENABLED=${ADAPTIVE_ENABLED:-1}

# The outer root overlay can be full even though /root is a large dedicated
# filesystem. Keep all temporary build/evaluation files on that filesystem.
TMPDIR=${TMPDIR:-$RUN_DIR/tmp}
mkdir -p "$TMPDIR"
export TMPDIR

if [[ "${BRT6_IN_ITERATIVE_CONTROLLER:-0}" != "1" ]]; then
  mkdir -p "$RUN_DIR"/{logs,evaluation,feedback,official_workspace}
  python3 "$PROJECT_ROOT/scripts/prepare_failure_directed_run.py" \
    --source-run "$SOURCE_RUN" --run-dir "$RUN_DIR" --expected-instances 276 \
    | tee "$RUN_DIR/logs/reuse_manifest.log"
  relay_pid=
  if ! timeout 2 bash -c '</dev/tcp/172.18.0.1/17890' 2>/dev/null; then
    /usr/bin/python3 "$PROJECT_ROOT/scripts/docker_proxy_relay.py" \
      --listen-host 172.18.0.1 --listen-port 17890 \
      --target-host 127.0.0.1 --target-port 7890 \
      >>"$RUN_DIR/logs/docker_proxy_relay.log" 2>&1 &
    relay_pid=$!
    trap 'kill "$relay_pid" 2>/dev/null || true' EXIT INT TERM
    sleep 1
    kill -0 "$relay_pid"
  fi
  SWT_ENV_COMPAT=1 bash "$PROJECT_ROOT/scripts/prepare_matplotlib_compat_envs.sh"
  if ! docker image inspect "$CONTROLLER_IMAGE" >/dev/null 2>&1; then
    docker build -t "$CONTROLLER_IMAGE" -f "$PROJECT_ROOT/docker/controller.Dockerfile" "$PROJECT_ROOT/docker"
  fi
  docker run --rm --network host \
    --name "brt6-iterative-$RUN_NAME" \
    -e HTTP_PROXY -e HTTPS_PROXY -e ALL_PROXY -e NO_PROXY \
    -e http_proxy -e https_proxy -e all_proxy -e no_proxy \
    -e TMPDIR \
    -e BRT6_IN_ITERATIVE_CONTROLLER=1 -e BRT6_IN_OFFICIAL_CONTROLLER=1 \
    -e BRT_API_POOL_FILE="$PROJECT_ROOT/.secrets/api_pool.json" \
    -e OFFICIAL_ROOT -e SOURCE_RUN -e RUN_NAME -e RUN_DIR \
    -e FEEDBACK_ROUNDS -e FEEDBACK_WORKERS -e REPAIR_WORKERS -e EVALUATION_WORKERS \
    -e EXECUTION_TIMEOUT -e MODEL -e INSTANCE_IDS_FILE \
    -e DIRECT_GENERATION -e PORTFOLIO_ENABLED -e SURROGATE_WORKERS -e ADAPTIVE_ENABLED \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$PACKAGE_ROOT:$PACKAGE_ROOT" \
    -v "$OFFICIAL_ROOT:$OFFICIAL_ROOT:ro" \
    -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
    -v "$PROJECT_ROOT/docker/official-docker-config:/root/.docker:ro" \
    -w "$PROJECT_ROOT" "$CONTROLLER_IMAGE" \
    bash "$PROJECT_ROOT/scripts/run_swt_iterative_docker_full.sh"
  exit $?
fi

INSTANCES=$PROJECT_ROOT/data/issues/swt276_issues.json
CODE_RETRIEVAL=$PROJECT_ROOT/retrieval_results/code/code_retrieval_results_gpt.json
TEST_RETRIEVAL=$PROJECT_ROOT/retrieval_results/test/icore/gpt/related_tests.json
REPO_ROOT=$PACKAGE_ROOT/swe_repos
ACTIVE_INSTANCES=$INSTANCES
if [[ -n "$INSTANCE_IDS_FILE" ]]; then
  ACTIVE_INSTANCES=$RUN_DIR/tmp/active_instances.json
  python3 - "$INSTANCES" "$INSTANCE_IDS_FILE" "$ACTIVE_INSTANCES" <<'PY'
import json
import sys
from pathlib import Path

rows = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
wanted = {
    line.strip()
    for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
    if line.strip()
}
selected = [row for row in rows if row.get("instance_id") in wanted]
found = {row["instance_id"] for row in selected}
missing = sorted(wanted - found)
if missing:
    raise SystemExit(f"instance IDs absent from SWT-276 dataset: {missing}")
Path(sys.argv[3]).parent.mkdir(parents=True, exist_ok=True)
Path(sys.argv[3]).write_text(
    json.dumps(selected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps({"requested": len(wanted), "selected": len(selected)}))
PY
fi
current_generation=$SOURCE_RUN/generation
candidate_args=(--candidate-generation "round0=$current_generation")
verification_args=()

for round in $(seq 1 "$FEEDBACK_ROUNDS"); do
  round_root=$RUN_DIR/feedback/round_$round
  predictions=$round_root/predictions.jsonl
  workspace=$round_root/official_workspace
  patch_id=docker_feedback_round_$round
  mkdir -p "$round_root" "$workspace"
  python3 "$PROJECT_ROOT/scripts/export_swt_predictions.py" \
    --instances "$ACTIVE_INSTANCES" --generation-dir "$current_generation" \
    --output "$predictions" --model-name brt6__DeepSeek-V4-Flash
  # Missing logs are isolated Docker/infrastructure failures.  Carry the
  # previous candidate forward instead of terminating the complete run.
  repair_args=(--allow-missing)
  if [[ "$round" -gt 1 ]]; then
    previous_summary=$RUN_DIR/feedback/round_$((round - 1))/generation/docker_feedback_summary.json
    ids_file=$round_root/instances_to_repair.txt
    python3 - "$previous_summary" "$ids_file" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1]))
ids = sorted(
    row["instance_id"]
    for row in data["results"]
    if row["action"] in {"repaired", "repair_failed", "fallback"}
)
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(ids) + ("\n" if ids else ""))
print(f"feedback_candidates={len(ids)}")
PY
    filtered=$round_root/predictions_repaired_only.jsonl
    python3 "$PROJECT_ROOT/scripts/filter_swt_predictions.py" \
      --predictions "$predictions" --instance-ids "$ids_file" --output "$filtered"
    predictions=$filtered
  fi
  if [[ ! -e "$workspace/dataset" ]]; then
    ln -s "$OFFICIAL_ROOT/dataset" "$workspace/dataset"
  fi
  (
    cd "$workspace"
    PYTHONPATH="$PROJECT_ROOT/scripts:$PROJECT_ROOT/scripts/official_runtime:$OFFICIAL_ROOT:$OFFICIAL_ROOT/src" \
      python3 "$PROJECT_ROOT/scripts/run_swt_pred_pre_feedback.py" \
      --predictions "$predictions" --run-id "$RUN_NAME-feedback-$round" \
      --patch-id "$patch_id" --max-workers "$FEEDBACK_WORKERS" --timeout "$EXECUTION_TIMEOUT"
  ) 2>&1 | tee "$RUN_DIR/logs/feedback_execution_round_$round.log"
  touch "$round_root/execution.done"
  next_generation=$round_root/generation
  PYTHONPATH="$PACKAGE_ROOT" python3 "$PROJECT_ROOT/scripts/repair_swt_from_buggy_feedback.py" \
    --instances "$ACTIVE_INSTANCES" --code-retrieval "$CODE_RETRIEVAL" \
    --source-generation "$current_generation" --output-generation "$next_generation" \
    --feedback-root "$workspace/run_instance_swt_logs/$RUN_NAME-feedback-$round/$patch_id" \
    --round "$round" --model "$MODEL" --max-workers "$REPAIR_WORKERS" \
    "${repair_args[@]}" \
    2>&1 | tee "$RUN_DIR/logs/feedback_repair_round_$round.log"
  touch "$round_root/repair.done"
  verification_args+=(--verification-summary "round$((round - 1))=$next_generation/docker_feedback_summary.json")
  candidate_args+=(--candidate-generation "round$round=$next_generation")
  current_generation=$next_generation
done

# The last repaired candidate has not executed yet. Verify it once on the
# buggy-side official Docker path, then apply the original BRT fallback idea
# conservatively: keep the newest accepted candidate, or fall back only from a
# non-executable/unverified regression. No fixed or golden information enters
# this selection.
verify_root=$RUN_DIR/feedback/final_verification
verify_workspace=$verify_root/official_workspace
verify_predictions=$verify_root/predictions.jsonl
verify_patch_id=docker_feedback_final_verification
mkdir -p "$verify_root" "$verify_workspace"
python3 "$PROJECT_ROOT/scripts/export_swt_predictions.py" \
  --instances "$ACTIVE_INSTANCES" --generation-dir "$current_generation" \
  --output "$verify_predictions" --model-name brt6__DeepSeek-V4-Flash
if [[ ! -e "$verify_workspace/dataset" ]]; then
  ln -s "$OFFICIAL_ROOT/dataset" "$verify_workspace/dataset"
fi
(
  cd "$verify_workspace"
  PYTHONPATH="$PROJECT_ROOT/scripts:$PROJECT_ROOT/scripts/official_runtime:$OFFICIAL_ROOT:$OFFICIAL_ROOT/src" \
    python3 "$PROJECT_ROOT/scripts/run_swt_pred_pre_feedback.py" \
    --predictions "$verify_predictions" --run-id "$RUN_NAME-feedback-final" \
    --patch-id "$verify_patch_id" --max-workers "$FEEDBACK_WORKERS" \
    --timeout "$EXECUTION_TIMEOUT"
) 2>&1 | tee "$RUN_DIR/logs/feedback_execution_final_verification.log"
verify_results=$verify_root/verifier
PYTHONPATH="$PACKAGE_ROOT" python3 "$PROJECT_ROOT/scripts/repair_swt_from_buggy_feedback.py" \
  --instances "$ACTIVE_INSTANCES" --code-retrieval "$CODE_RETRIEVAL" \
  --source-generation "$current_generation" --output-generation "$verify_results" \
  --feedback-root "$verify_workspace/run_instance_swt_logs/$RUN_NAME-feedback-final/$verify_patch_id" \
  --round "$((FEEDBACK_ROUNDS + 1))" --model "$MODEL" --max-workers "$REPAIR_WORKERS" \
  --verify-only --allow-missing \
  2>&1 | tee "$RUN_DIR/logs/feedback_verify_final.log"
touch "$verify_root/verification.done"
verification_args+=(--verification-summary "round$FEEDBACK_ROUNDS=$verify_results/docker_feedback_summary.json")

# Restore BRT5 adaptive top-3 without tripling every model call.  Only
# instances for which seed0 plus the complete repair budget produced no
# semantically accepted buggy failure receive seed1, then seed2 if necessary.
# All earlier checkpoints remain candidates, so a weaker fallback seed cannot
# erase a real buggy failure.  This phase remains buggy-only.
adaptive_root=$RUN_DIR/feedback/adaptive_seeds
mkdir -p "$adaptive_root"
accepted_summaries=()
for round in $(seq 1 "$FEEDBACK_ROUNDS"); do
  accepted_summaries+=("$RUN_DIR/feedback/round_$round/generation/docker_feedback_summary.json")
done
accepted_summaries+=("$verify_results/docker_feedback_summary.json")

write_unaccepted_ids() {
  local instances_json=$1
  local output_ids=$2
  shift 2
  python3 - "$instances_json" "$output_ids" "$@" <<'PY'
import json
import sys
from pathlib import Path

instances = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
accepted = set()
for raw in sys.argv[3:]:
    path = Path(raw)
    if not path.is_file():
        continue
    for row in json.loads(path.read_text(encoding="utf-8")).get("results", []):
        if row.get("action") == "accepted" and row.get("status") not in {
            "PASS", "BUGGY_PASS", "SETUP_ERROR", "SYNTAX_ERROR",
            "COLLECT_ERROR", "TIMEOUT",
        }:
            accepted.add(row["instance_id"])
missing = [row["instance_id"] for row in instances if row["instance_id"] not in accepted]
Path(sys.argv[2]).write_text("\n".join(missing) + ("\n" if missing else ""), encoding="utf-8")
print(json.dumps({"accepted": len(instances) - len(missing), "adaptive_recovery": len(missing)}))
PY
}

materialize_subset() {
  local instances_json=$1
  local ids_file=$2
  local output_json=$3
  python3 - "$instances_json" "$ids_file" "$output_json" <<'PY'
import json
import sys
from pathlib import Path
rows = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
wanted = {x.strip() for x in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines() if x.strip()}
selected = [row for row in rows if row["instance_id"] in wanted]
Path(sys.argv[3]).write_text(json.dumps(selected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

recover_seed() {
  local seed_index=$1
  local ids_file=$2
  local seed_root=$adaptive_root/seed_$seed_index
  local subset=$seed_root/instances.json
  local generation=$seed_root/generation
  local predictions=$seed_root/predictions.jsonl
  local workspace=$seed_root/official_workspace
  local verifier=$seed_root/verifier
  local patch_id=adaptive_seed_$seed_index
  mkdir -p "$seed_root" "$workspace"
  materialize_subset "$ACTIVE_INSTANCES" "$ids_file" "$subset"
  if [[ ! -s "$ids_file" ]]; then
    return 0
  fi
  (
    cd "$PACKAGE_ROOT"
    BRT4_BEHAVIOR_CACHE_DIR="$SOURCE_RUN/generation" \
      python3 -m brt6.pipeline.run \
      --instances_path "$subset" \
      --code_retrieval_path "$CODE_RETRIEVAL" \
      --test_retrieval_path "$TEST_RETRIEVAL" \
      --repo_root_base "$REPO_ROOT" --output_dir "$generation" \
      --llm-provider deepseek --model "$MODEL" --max_workers "$REPAIR_WORKERS" \
      --temperature 0.1 --max_tokens 4096 \
      --runtime_backend swt_bench_docker --no_conda --generate_only \
      --forced_seed_index "$seed_index" --timeout "$EXECUTION_TIMEOUT"
  ) 2>&1 | tee "$RUN_DIR/logs/adaptive_seed_${seed_index}_generation.log"
  python3 "$PROJECT_ROOT/scripts/export_swt_predictions.py" \
    --instances "$subset" --generation-dir "$generation" \
    --output "$predictions" --model-name brt6__DeepSeek-V4-Flash
  [[ -e "$workspace/dataset" ]] || ln -s "$OFFICIAL_ROOT/dataset" "$workspace/dataset"
  (
    cd "$workspace"
    PYTHONPATH="$PROJECT_ROOT/scripts:$PROJECT_ROOT/scripts/official_runtime:$OFFICIAL_ROOT:$OFFICIAL_ROOT/src" \
      python3 "$PROJECT_ROOT/scripts/run_swt_pred_pre_feedback.py" \
      --predictions "$predictions" --run-id "$RUN_NAME-$patch_id" \
      --patch-id "$patch_id" --max-workers "$FEEDBACK_WORKERS" \
      --timeout "$EXECUTION_TIMEOUT"
  ) 2>&1 | tee "$RUN_DIR/logs/adaptive_seed_${seed_index}_execution.log"
  PYTHONPATH="$PACKAGE_ROOT" python3 "$PROJECT_ROOT/scripts/repair_swt_from_buggy_feedback.py" \
    --instances "$subset" --code-retrieval "$CODE_RETRIEVAL" \
    --source-generation "$generation" --output-generation "$verifier" \
    --feedback-root "$workspace/run_instance_swt_logs/$RUN_NAME-$patch_id/$patch_id" \
    --round "$((FEEDBACK_ROUNDS + 1 + seed_index))" --model "$MODEL" \
    --max-workers "$REPAIR_WORKERS" --verify-only --allow-missing \
    2>&1 | tee "$RUN_DIR/logs/adaptive_seed_${seed_index}_verify.log"
  candidate_args+=(--candidate-generation "seed$seed_index=$generation")
  verification_args+=(--verification-summary "seed$seed_index=$verifier/docker_feedback_summary.json")
}

seed1_ids=$adaptive_root/seed_1_ids.txt
if [[ "$ADAPTIVE_ENABLED" == "1" ]]; then
  write_unaccepted_ids "$ACTIVE_INSTANCES" "$seed1_ids" "${accepted_summaries[@]}"
  recover_seed 1 "$seed1_ids"
  seed1_summary=$adaptive_root/seed_1/verifier/docker_feedback_summary.json
  if [[ -f "$seed1_summary" ]]; then
    seed2_ids=$adaptive_root/seed_2_ids.txt
    write_unaccepted_ids "$ACTIVE_INSTANCES" "$seed2_ids" "${accepted_summaries[@]}" "$seed1_summary"
    recover_seed 2 "$seed2_ids"
  fi
fi

primary_generation=$RUN_DIR/feedback/primary_selected_generation
python3 "$PROJECT_ROOT/scripts/select_swt_verified_fallback.py" \
  --instances "$ACTIVE_INSTANCES" \
  "${candidate_args[@]}" \
  "${verification_args[@]}" \
  --output-generation "$primary_generation" \
  --manifest "$RUN_DIR/evaluation/primary_verified_fallback_selection.json" \
  2>&1 | tee "$RUN_DIR/logs/verified_fallback_selection.log"

current_generation=$primary_generation
if [[ "$PORTFOLIO_ENABLED" == "1" && -n "$DIRECT_GENERATION" ]]; then
  portfolio_root=$RUN_DIR/feedback/issue_only_portfolio
  mkdir -p "$portfolio_root"

  run_portfolio_buggy() {
    local label=$1
    local generation=$2
    local root=$portfolio_root/$label
    local workspace=$root/workspace
    local predictions=$root/predictions.jsonl
    local run_id=$RUN_NAME-portfolio-$label
    local patch_id=portfolio-$label
    mkdir -p "$workspace"
    python3 "$PROJECT_ROOT/scripts/export_swt_predictions.py" \
      --instances "$ACTIVE_INSTANCES" --generation-dir "$generation" \
      --output "$predictions" --model-name brt6__DeepSeek-V4-Flash
    [[ -e "$workspace/dataset" ]] || ln -s "$OFFICIAL_ROOT/dataset" "$workspace/dataset"
    (
      cd "$workspace"
      PYTHONPATH="$PROJECT_ROOT/scripts:$PROJECT_ROOT/scripts/official_runtime:$OFFICIAL_ROOT:$OFFICIAL_ROOT/src" \
        python3 "$PROJECT_ROOT/scripts/run_swt_pred_pre_feedback.py" \
        --predictions "$predictions" --run-id "$run_id" --patch-id "$patch_id" \
        --max-workers "$FEEDBACK_WORKERS" --timeout "$EXECUTION_TIMEOUT"
    ) 2>&1 | tee "$RUN_DIR/logs/portfolio_${label}_buggy.log"
    PYTHONPATH="$PACKAGE_ROOT:$PROJECT_ROOT/scripts" \
      python3 "$PROJECT_ROOT/scripts/summarize_swt_buggy_execution.py" \
      --instances "$ACTIVE_INSTANCES" --generation-dir "$generation" \
      --feedback-root "$workspace/run_instance_swt_logs/$run_id/$patch_id" \
      --output "$root/buggy_execution_summary.json"
  }

  run_portfolio_surrogate() {
    local label=$1
    local generation=$2
    local root=$portfolio_root/$label
    local surrogate=$root/surrogate
    local workspace=$surrogate/workspace
    local predictions=$surrogate/predictions.jsonl
    local run_id=$RUN_NAME-surrogate-$label
    local patch_id=surrogate-$label
    mkdir -p "$workspace"
    PYTHONPATH="$PACKAGE_ROOT:$PROJECT_ROOT/scripts" \
      python3 "$PROJECT_ROOT/scripts/generate_swt_surrogate_predictions.py" \
      --instances "$ACTIVE_INSTANCES" --code-retrieval "$CODE_RETRIEVAL" \
      --repo-root "$REPO_ROOT" --generation-dir "$generation" \
      --verification-summary "$root/buggy_execution_summary.json" \
      --feedback-root "$root/workspace/run_instance_swt_logs/$RUN_NAME-portfolio-$label/portfolio-$label" \
      --output-dir "$surrogate" --predictions "$predictions" \
      --model "$MODEL" --max-workers "$SURROGATE_WORKERS" --max-rounds 2
    [[ -e "$workspace/dataset" ]] || ln -s "$OFFICIAL_ROOT/dataset" "$workspace/dataset"
    if [[ -s "$predictions" ]]; then
      (
        cd "$workspace"
        PYTHONPATH="$PROJECT_ROOT/scripts:$PROJECT_ROOT/scripts/official_runtime:$OFFICIAL_ROOT:$OFFICIAL_ROOT/src" \
          python3 "$PROJECT_ROOT/scripts/run_swt_pred_pre_feedback.py" \
          --predictions "$predictions" --run-id "$run_id" --patch-id "$patch_id" \
          --max-workers "$FEEDBACK_WORKERS" --timeout "$EXECUTION_TIMEOUT"
      ) 2>&1 | tee "$RUN_DIR/logs/portfolio_${label}_surrogate.log"
    fi
    PYTHONPATH="$PACKAGE_ROOT:$PROJECT_ROOT/scripts" \
      python3 "$PROJECT_ROOT/scripts/summarize_swt_surrogate.py" \
      --generation-summary "$surrogate/surrogate_generation_summary.json" \
      --feedback-root "$workspace/run_instance_swt_logs/$run_id/$patch_id" \
      --output "$surrogate/surrogate_execution_summary.json"
  }

  run_portfolio_buggy primary "$primary_generation"
  run_portfolio_buggy direct "$DIRECT_GENERATION"
  run_portfolio_surrogate primary "$primary_generation"
  run_portfolio_surrogate direct "$DIRECT_GENERATION"

  selected_generation=$RUN_DIR/feedback/selected_generation
  python3 "$PROJECT_ROOT/scripts/select_swt_verified_fallback.py" \
    --instances "$ACTIVE_INSTANCES" \
    --candidate-generation "primary=$primary_generation" \
    --candidate-generation "direct=$DIRECT_GENERATION" \
    --verification-summary "primary=$portfolio_root/primary/buggy_execution_summary.json" \
    --verification-summary "direct=$portfolio_root/direct/buggy_execution_summary.json" \
    --surrogate-summary "primary=$portfolio_root/primary/surrogate/surrogate_execution_summary.json" \
    --surrogate-summary "direct=$portfolio_root/direct/surrogate/surrogate_execution_summary.json" \
    --output-generation "$selected_generation" \
    --manifest "$RUN_DIR/evaluation/verified_fallback_selection.json" \
    2>&1 | tee "$RUN_DIR/logs/portfolio_selection.log"
  current_generation=$selected_generation
else
  cp "$RUN_DIR/evaluation/primary_verified_fallback_selection.json" \
    "$RUN_DIR/evaluation/verified_fallback_selection.json"
fi
touch "$RUN_DIR/evaluation/selection.done"

final_predictions=$RUN_DIR/evaluation/predictions.jsonl
python3 "$PROJECT_ROOT/scripts/export_swt_predictions.py" \
  --instances "$ACTIVE_INSTANCES" --generation-dir "$current_generation" \
  --output "$final_predictions" --model-name brt6__DeepSeek-V4-Flash

RUN_NAME=$RUN_NAME RUN_DIR=$RUN_DIR SWT_RUN_ID=$RUN_NAME SWT_ENV_COMPAT=1 \
PREDICTIONS=$final_predictions EVALUATION_WORKERS=$EVALUATION_WORKERS \
EVALUATION_TIMEOUT=$EXECUTION_TIMEOUT \
  bash "$PROJECT_ROOT/scripts/run_swt_official_clean_full.sh"

python3 "$PROJECT_ROOT/scripts/summarize_failure_directed_run.py" \
  --run-dir "$RUN_DIR" --baseline-run "$SOURCE_RUN" \
  | tee "$RUN_DIR/logs/failure_directed_summary.log"
