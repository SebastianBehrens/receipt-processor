#!/bin/bash
set -o errexit
set -o pipefail
set -o nounset

IMAGE_TAG="${1:-latest}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "$PWD" != "$SCRIPT_DIR" ]]; then
  echo "Error: run this script from the deploy directory:"
  echo "  cd $SCRIPT_DIR"
  echo "  ./build.sh [tag]"
  exit 1
fi

# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"
if [[ -f "$SCRIPT_DIR/config.local.sh" ]]; then
  # shellcheck source=config.local.sh
  source "$SCRIPT_DIR/config.local.sh"
fi

DOCKER_HOST="ssh://${BUILDER_SSH_HOST}"
IMAGE="${REGISTRY}/${IMAGE_PREFIX}:${IMAGE_TAG}"

export DOCKER_HOST

echo "=========================================="
echo "Building receipt-processor image"
echo "DOCKER_HOST: $DOCKER_HOST"
echo "Image:       $IMAGE"
echo "Platform:    $PLATFORM"
echo "Builder:     $BUILDX_BUILDER"
echo "=========================================="

if ! docker buildx inspect "$BUILDX_BUILDER" >/dev/null 2>&1; then
  echo "Error: buildx builder '$BUILDX_BUILDER' not found."
  echo "Create it with:"
  echo "  docker buildx create --name $BUILDX_BUILDER \\"
  echo "    --driver docker-container \\"
  echo "    --platform linux/amd64 \\"
  echo "    --node homelab-amd64 \\"
  echo "    ssh://${BUILDER_SSH_HOST}"
  exit 1
fi

echo "Using builder: $BUILDX_BUILDER"
docker buildx use "$BUILDX_BUILDER"

docker buildx build \
  --platform "$PLATFORM" \
  --push \
  -t "$IMAGE" \
  -f "$REPO_ROOT/Dockerfile" \
  "$REPO_ROOT"

echo ""
echo "Image pushed: $IMAGE"
echo "Deploy from this directory:"
echo "  ./deploy.sh $IMAGE_TAG"
