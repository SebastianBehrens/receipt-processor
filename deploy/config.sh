# Receipt Processor — homelab configuration for build.sh and deploy.sh
#
# Edit values here, or create config.local.sh (gitignored) to override without
# changing this file.

REGISTRY="registry.epsian.ch"
IMAGE_PREFIX="receipt-processor"
STACK_NAME="receipt-processor"

BUILDER_SSH_HOST="root-homelab-builder"
BUILDX_BUILDER="homelab-builder"
GATEWAY_SSH_HOST="root-homelab-gateway"
SWARM_NODE="homelab-production1"
PROD_SSH_HOST="root-homelab-prod1"

DOCKER_DATA_DIR="/mnt/volume1/docker-data"
COMPOSE_FILE="swarm-compose.yml"
PLATFORM="linux/amd64"

# 1Password — override in config.local.sh if needed
OP_VAULT="Personal"
OP_ITEM="gemini-key-playground"
