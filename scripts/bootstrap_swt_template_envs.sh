#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
WORKSPACE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)

# Deliberately pin an installer older than the broken py314/26.5.3 base seen
# on server C.  Install into a separate prefix; never mutate /root/miniconda3.
CONDA_ROOT=${BRT_SWT_CONDA_ROOT:-$HOME/brt5-conda-25.11.1}
INSTALLER=Miniconda3-py312_25.11.1-1-Linux-x86_64.sh
INSTALLER_URL=https://repo.anaconda.com/miniconda/$INSTALLER
INSTALLER_SHA256=498ddb7c091002e4fd76e3496d91d2d915b183d1d850bef6e060fd45e2523213
INSTALL_SYSTEM_PACKAGES=true
PREWARM=true

usage() {
  cat <<'EOF'
Usage: bash scripts/bootstrap_swt_template_envs.sh [options]

Creates an isolated, pinned Conda installation and serially prebuilds all SWT
dependency-template environments using brt5's production environment code.

Options:
  --conda-root PATH          Dedicated Conda prefix.
  --skip-system-packages    Skip apt-get build dependencies.
  --skip-prewarm            Prepare controller/repositories only.
  -h, --help                Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --conda-root) CONDA_ROOT=${2:?missing path}; shift 2 ;;
    --conda-root=*) CONDA_ROOT=${1#*=}; shift ;;
    --skip-system-packages) INSTALL_SYSTEM_PACKAGES=false; shift ;;
    --skip-prewarm) PREWARM=false; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Linux" || "$(uname -m)" != "x86_64" ]]; then
  echo "This pinned bootstrap currently supports Linux x86_64 only." >&2
  exit 2
fi

if [[ "$INSTALL_SYSTEM_PACKAGES" == "true" ]]; then
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "apt-get is required; install equivalent build tools or use --skip-system-packages." >&2
    exit 2
  fi
  if [[ "$(id -u)" -eq 0 ]]; then
    ROOT_COMMAND=()
  elif command -v sudo >/dev/null 2>&1; then
    ROOT_COMMAND=(sudo)
  else
    echo "Root or sudo is required for system packages." >&2
    exit 2
  fi
  "${ROOT_COMMAND[@]}" apt-get update
  "${ROOT_COMMAND[@]}" apt-get install -y --no-install-recommends \
    bash build-essential ca-certificates curl git gfortran libffi-dev \
    libfreetype6-dev libjpeg-dev liblapack-dev libopenblas-dev libpng-dev \
    libssl-dev libxml2-dev libxslt1-dev pkg-config rsync
fi

DOWNLOAD_ROOT=$PROJECT_ROOT/.bootstrap/downloads
mkdir -p "$DOWNLOAD_ROOT"
INSTALLER_PATH=$DOWNLOAD_ROOT/$INSTALLER
if [[ ! -f "$INSTALLER_PATH" ]]; then
  curl --fail --location --retry 5 --retry-all-errors \
    --connect-timeout 30 --output "$INSTALLER_PATH" "$INSTALLER_URL"
fi
printf '%s  %s\n' "$INSTALLER_SHA256" "$INSTALLER_PATH" | sha256sum --check --status

if [[ ! -x "$CONDA_ROOT/bin/conda" ]]; then
  if [[ -e "$CONDA_ROOT" ]]; then
    echo "Refusing to overwrite incomplete Conda prefix: $CONDA_ROOT" >&2
    echo "Move or remove that dedicated prefix, then rerun this script." >&2
    exit 2
  fi
  bash "$INSTALLER_PATH" -b -p "$CONDA_ROOT"
fi

# Discard activation state inherited from the broken /root/miniconda3 shell.
unset CONDA_PREFIX CONDA_PREFIX_1 CONDA_PREFIX_2 CONDA_PREFIX_3
unset CONDA_PREFIX_4 CONDA_PREFIX_5 CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER
unset CONDA_SHLVL CONDA_PYTHON_EXE _CE_CONDA _CE_M PYTHONSTARTUP
export CONDA_EXE=$CONDA_ROOT/bin/conda
export BRT3_CONDA_SH=$CONDA_ROOT/etc/profile.d/conda.sh
export PATH=$CONDA_ROOT/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONPATH=$WORKSPACE_ROOT

"$CONDA_EXE" config --system --set auto_activate_base false
"$CONDA_EXE" config --system --set number_channel_notices 0
"$CONDA_EXE" config --system --set remote_max_retries 5

if "$CONDA_EXE" tos --help >/dev/null 2>&1; then
  "$CONDA_EXE" tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
  "$CONDA_EXE" tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r
fi

PROBE_ENV=brt5_conda_probe
"$CONDA_EXE" env remove -n "$PROBE_ENV" -y >/dev/null 2>&1 || true
"$CONDA_EXE" create -n "$PROBE_ENV" python=3.9 -y
"$CONDA_EXE" run -n "$PROBE_ENV" python -c 'import sys; assert sys.version_info[:2] == (3, 9); print(sys.version)'
"$CONDA_EXE" env remove -n "$PROBE_ENV" -y

if "$CONDA_EXE" run -n icore python -c 'import sys' >/dev/null 2>&1; then
  "$CONDA_EXE" install -n icore -y python=3.12 pip
else
  "$CONDA_EXE" create -n icore -y python=3.12 pip
fi
PYTHON_BIN=$CONDA_ROOT/envs/icore/bin/python
"$PYTHON_BIN" -m pip install pip==26.1.2 setuptools==82.0.1 wheel==0.47.0
"$PYTHON_BIN" -m pip install -r "$PROJECT_ROOT/requirements.txt"

mkdir -p "$PROJECT_ROOT/.bootstrap"
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/bootstrap_repositories.py" \
  --dataset "$PROJECT_ROOT/data/issues/swt276_issues.json" \
  --repo-root "$WORKSPACE_ROOT/swe_repos" \
  --manifest "$PROJECT_ROOT/.bootstrap/repositories.json"

if [[ "$PREWARM" == "true" ]]; then
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/prewarm_swt_environments.py" \
    --dataset "$PROJECT_ROOT/data/issues/swt276_issues.json" \
    --work-root "$PROJECT_ROOT/.bootstrap/swt-template-environments" \
    --timeout 3600 \
    --retries 3
fi

ENV_FILE=$PROJECT_ROOT/.bootstrap/use_swt_conda.sh
cat > "$ENV_FILE" <<EOF
unset CONDA_PREFIX CONDA_PREFIX_1 CONDA_PREFIX_2 CONDA_PREFIX_3
unset CONDA_PREFIX_4 CONDA_PREFIX_5 CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER
unset CONDA_SHLVL CONDA_PYTHON_EXE _CE_CONDA _CE_M PYTHONSTARTUP
export CONDA_EXE='$CONDA_ROOT/bin/conda'
export BRT3_CONDA_SH='$CONDA_ROOT/etc/profile.d/conda.sh'
export PYTHON_BIN='$CONDA_ROOT/envs/icore/bin/python'
export PATH='$CONDA_ROOT/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
export PYTHONPATH='$WORKSPACE_ROOT'
EOF
chmod 600 "$ENV_FILE"

cat <<EOF

SWT template environment bootstrap completed.
Dedicated Conda: $CONDA_ROOT
Environment summary: $PROJECT_ROOT/.bootstrap/swt-template-environments/summary.json

Before every experiment, run:
  source $ENV_FILE

Then launch, for example:
  bash $PROJECT_ROOT/scripts/run_p0_simple_llm_selector_full.sh --dataset swt --behavior-target off
EOF
