#!/usr/bin/env bash
# Create receipt-processor data directories on homelab-production1.
#
# Run once before the first deploy.
#
# Usage:
#   ./prepare-directories.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"
if [[ -f "$SCRIPT_DIR/config.local.sh" ]]; then
  # shellcheck source=config.local.sh
  source "$SCRIPT_DIR/config.local.sh"
fi

SSH_HOST="${PROD_SSH_HOST:-root-homelab-prod1}"
DATA_DIR="${DOCKER_DATA_DIR}/receipt-processor"
APP_UID="${APP_UID:-3002}"
APP_GID="${APP_GID:-3002}"

log() {
  printf '[receipt-processor-prepare] %s\n' "$*"
}

log "Creating directories on ${SSH_HOST}"
log "DATA_DIR=${DATA_DIR}"
log "owner ${APP_UID}:${APP_GID}"

ssh "${SSH_HOST}" bash -s -- "${DATA_DIR}" "${APP_UID}" "${APP_GID}" <<'EOF'
set -euo pipefail

data_dir="$1"
uid="$2"
gid="$3"

mkdir -p "${data_dir}/data" "${data_dir}/logs" "${data_dir}/media"
touch "${data_dir}/db.sqlite3"
chown -R "${uid}:${gid}" "${data_dir}"

printf 'created %s\n' "${data_dir}"
find "${data_dir}" -maxdepth 2 -printf '%M %u:%g %p\n'
EOF

log "done"
