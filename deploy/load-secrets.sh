#!/usr/bin/env bash
# Load deploy secrets from 1Password and generate the Django secret key.
#
# Usage (from deploy/):
#   ./load-secrets.sh <secrets-dir>
#
# Writes:
#   <secrets-dir>/gemini_api_key.txt     — from 1Password item gemini-key-receipt-processor
#   <secrets-dir>/django_secret_key.txt  — htpasswd-generated, persisted in .django_secret_key

set -euo pipefail

SECRETS_DIR="${1:?Usage: load-secrets.sh <secrets-dir>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"
if [[ -f "$SCRIPT_DIR/config.local.sh" ]]; then
  # shellcheck source=config.local.sh
  source "$SCRIPT_DIR/config.local.sh"
fi

: "${OP_VAULT:=Personal}"
: "${OP_ITEM:=gemini-key-receipt-processor}"

require_op() {
  if ! command -v op >/dev/null 2>&1; then
    echo "Error: 1Password CLI (op) not found." >&2
    exit 1
  fi
  if ! op whoami >/dev/null 2>&1; then
    echo "Error: not signed in to 1Password. Run:" >&2
    echo '  eval "$(op signin)"' >&2
    exit 1
  fi
}

read_op_field() {
  local field="$1"
  if [[ -n "${OP_VAULT_ID:-}" ]]; then
    op read "op://${OP_VAULT}/${OP_ITEM}/${field}" 2>/dev/null \
      || op read "op://${OP_VAULT_ID}/${OP_ITEM}/${field}"
  else
    op read "op://${OP_VAULT}/${OP_ITEM}/${field}"
  fi
}

read_gemini_api_key() {
  local field
  for field in credential password GEMINI_API_KEY; do
    if read_op_field "$field" >/dev/null 2>&1; then
      read_op_field "$field"
      return
    fi
  done
  echo "Error: could not read credential, password, or GEMINI_API_KEY from 1Password item '${OP_ITEM}' in vault '${OP_VAULT}'." >&2
  exit 1
}

generate_django_secret_key() {
  if ! command -v htpasswd >/dev/null 2>&1; then
    echo "Error: htpasswd not found (install httpd or apache2-utils)." >&2
    exit 1
  fi
  htpasswd -nbBC 10 django "$(openssl rand -base64 48)" | cut -d: -f2-
}

require_op
mkdir -p "$SECRETS_DIR"

printf '[load-secrets] Loading 1Password item %q from vault %q\n' "$OP_ITEM" "$OP_VAULT"
printf '%s' "$(read_gemini_api_key)" > "${SECRETS_DIR}/gemini_api_key.txt"

LOCAL_SECRET_KEY="${SCRIPT_DIR}/.django_secret_key"
if [[ ! -f "$LOCAL_SECRET_KEY" ]]; then
  printf '[load-secrets] Generating Django secret key with htpasswd\n'
  generate_django_secret_key > "$LOCAL_SECRET_KEY"
  chmod 600 "$LOCAL_SECRET_KEY"
fi
cp "$LOCAL_SECRET_KEY" "${SECRETS_DIR}/django_secret_key.txt"

chmod 600 "${SECRETS_DIR}/gemini_api_key.txt" "${SECRETS_DIR}/django_secret_key.txt"
printf '[load-secrets] wrote secrets to %s\n' "$SECRETS_DIR"
