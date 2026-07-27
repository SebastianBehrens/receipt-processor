#!/bin/bash
set -o errexit
set -o pipefail
set -o nounset

IMAGE_TAG="${1:-latest}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$PWD" != "$SCRIPT_DIR" ]]; then
  echo "Error: run this script from the deploy directory:"
  echo "  cd $SCRIPT_DIR"
  echo "  ./deploy.sh [tag]"
  exit 1
fi

# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"
if [[ -f "$SCRIPT_DIR/config.local.sh" ]]; then
  # shellcheck source=config.local.sh
  source "$SCRIPT_DIR/config.local.sh"
fi

DOCKER_HOST="ssh://${GATEWAY_SSH_HOST}"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Error: compose file not found: $SCRIPT_DIR/$COMPOSE_FILE"
  exit 1
fi

SECRETS_DIR="${SCRIPT_DIR}/secrets"
COMPOSE_TMP="$(mktemp "${TMPDIR:-/tmp}/receipt-processor-compose.XXXXXX")"
cleanup() {
  rm -rf "$SECRETS_DIR"
  rm -f "$COMPOSE_TMP"
}
trap cleanup EXIT

"$SCRIPT_DIR/load-secrets.sh" "$SECRETS_DIR"

GEMINI_FILE="${SECRETS_DIR}/gemini_api_key.txt"
SECRET_KEY_FILE="${SECRETS_DIR}/django_secret_key.txt"

sed \
  -e "s|file: ./gemini_api_key.txt|file: ${GEMINI_FILE}|" \
  -e "s|file: ./django_secret_key.txt|file: ${SECRET_KEY_FILE}|" \
  "$SCRIPT_DIR/$COMPOSE_FILE" > "$COMPOSE_TMP"

export DOCKER_HOST
export DOCKER_DATA_DIR
export REGISTRY
export IMAGE_PREFIX
export IMAGE_TAG
export SWARM_NODE

SECRET_PREFIX="${STACK_NAME}_"
SERVICE_NAME="${STACK_NAME}_app"

rotate_secret_if_changed() {
  local secret_name="$1"
  local secret_file="$2"
  local hash_file="${SCRIPT_DIR}/.${secret_name}-deployed.sha256"
  local swarm_secret="${SECRET_PREFIX}${secret_name}"
  local current_hash deployed_hash

  current_hash="$(shasum -a 256 "$secret_file" | awk '{print $1}')"
  deployed_hash=""
  if [[ -f "$hash_file" ]]; then
    deployed_hash="$(< "$hash_file")"
  fi

  if docker secret inspect "$swarm_secret" >/dev/null 2>&1 && [[ "$current_hash" != "$deployed_hash" ]]; then
    echo "Rotating Swarm secret ${swarm_secret} ..."
    docker service scale "${SERVICE_NAME}=0"
    for _ in $(seq 1 30); do
      replicas="$(docker service ls --filter "name=${SERVICE_NAME}" --format '{{.Replicas}}' 2>/dev/null || true)"
      if [[ "$replicas" == "0/0" ]]; then
        break
      fi
      sleep 2
    done
    docker secret rm "$swarm_secret"
  fi

  echo "$current_hash" > "$hash_file"
}

rotate_secret_if_changed "gemini_api_key" "$GEMINI_FILE"
rotate_secret_if_changed "django_secret_key" "$SECRET_KEY_FILE"

echo "=========================================="
echo "Deploying receipt-processor stack"
echo "DOCKER_HOST:     $DOCKER_HOST"
echo "DOCKER_DATA_DIR: $DOCKER_DATA_DIR"
echo "Stack:           $STACK_NAME"
echo "Node:            $SWARM_NODE"
echo "Compose file:    $COMPOSE_FILE (with temp secret paths)"
echo "Image:           ${REGISTRY}/${IMAGE_PREFIX}:${IMAGE_TAG}"
echo "Data mount:      ${DOCKER_DATA_DIR}/receipt-processor/"
echo "Secrets:         from 1Password item '${OP_ITEM:-gemini-key-receipt-processor}' (vault ${OP_VAULT:-Personal})"
echo "=========================================="

docker stack deploy --with-registry-auth -c "$COMPOSE_TMP" "$STACK_NAME"

echo ""
echo "Stack deployed: $STACK_NAME"
echo "Check services:"
echo "  docker stack services $STACK_NAME"
