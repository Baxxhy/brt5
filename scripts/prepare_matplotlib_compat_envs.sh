#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
DOCKERFILE=$PROJECT_ROOT/docker/matplotlib-compat-env.Dockerfile

SOURCES=(
  exec.env.x86_64.657279c2e5581771ccbc6f:latest
  exec.env.x86_64.1888cbc29e8911ba248a1e:latest
  exec.env.x86_64.bc7872d535a60a6ea04d27:latest
  exec.env.x86_64.89beb6334e800c4362eea6:latest
)

for source in "${SOURCES[@]}"; do
  target="brt6.compat.${source}"
  if docker image inspect "$target" >/dev/null 2>&1; then
    echo "Compatibility env already exists: $target"
    continue
  fi
  docker image inspect "$source" >/dev/null
  docker build \
    --network host \
    --build-arg "SOURCE_IMAGE=$source" \
    --tag "$target" \
    --file "$DOCKERFILE" \
    "$PROJECT_ROOT/docker"
done
