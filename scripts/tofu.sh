#!/usr/bin/env bash
# Run OpenTofu against one stack and one workspace, with credentials resolved
# from Bitwarden Secrets Manager at the moment of use.
#
#   scripts/tofu.sh hetzner hetzner plan
#   scripts/tofu.sh proxmox pve01   apply -auto-approve
#   scripts/tofu.sh cloudflare default plan
#
# Arguments: <stack> <workspace> <tofu subcommand and flags...>
#
# The stack is a directory under tofu/. The workspace is the hosts/ folder the
# run is for (Cloudflare has only one, called "default"), which keeps one
# Proxmox node's state independent of another's.
#
# State lives in an HTTP backend, addressed per stack and workspace. Nothing
# about the backend is committed: the URL and its credentials are secrets too.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS="${REPO_ROOT}/scripts/secrets.sh"

# Bitwarden keys. Override per environment if your naming differs.
: "${TF_STATE_URL_KEY:=tofu/state_url}"
: "${TF_STATE_USER_KEY:=tofu/state_username}"
: "${TF_STATE_PASSWORD_KEY:=tofu/state_password}"

die() { echo "tofu.sh: $*" >&2; exit 1; }

[[ $# -ge 3 ]] || die "usage: tofu.sh <stack> <workspace> <command...>"

STACK="$1"; shift
WORKSPACE="$1"; shift

STACK_DIR="${REPO_ROOT}/tofu/${STACK}"
[[ -d "$STACK_DIR" ]] || die "no such stack: tofu/${STACK}"

VAR_FILE="${STACK_DIR}/vars/${WORKSPACE}.tfvars.json"

# Credentials the stack itself needs, as TF_VAR_* env vars. Each is looked up
# only for the stack that declares it, so a DNS run never holds a cloud token.
# A host with no public SSH has exactly one way in, and it has to exist before
# anything can reach the machine — so a provisioning run carries a short-lived
# Tailscale key for cloud-init to register with. Existing hosts ignore changes
# to user_data, so re-minting does not churn the fleet.
mint_tailscale_key() {
  local tailnet_file="${REPO_ROOT}/tailscale/tailnet.yml"
  [[ -f "$tailnet_file" ]] || return 0
  case "${1:-}" in
    plan | apply) ;;
    *) return 0 ;;
  esac
  TF_VAR_tailscale_auth_key="$("${REPO_ROOT}/scripts/tailscale_authkey.sh")"
  TF_VAR_tailscale_tags="$(python3 -c "
import yaml
config = yaml.safe_load(open('${tailnet_file}'))
print((config.get('tags') or {}).get('server') or 'tag:server')
")"
  export TF_VAR_tailscale_auth_key TF_VAR_tailscale_tags
}

case "$STACK" in
  hetzner)
    TF_VAR_hcloud_token="$("$SECRETS" get "${HCLOUD_TOKEN_KEY:-hetzner/api_token}")"
    export TF_VAR_hcloud_token
    mint_tailscale_key "${1:-}"
    ;;
  proxmox)
    TF_VAR_proxmox_api_token="$("$SECRETS" get "${PROXMOX_TOKEN_KEY:-proxmox/${WORKSPACE}_api_token}")"
    TF_VAR_proxmox_ssh_password="$("$SECRETS" get "${PROXMOX_SSH_PASSWORD_KEY:-proxmox/${WORKSPACE}_root_password}")"
    export TF_VAR_proxmox_api_token TF_VAR_proxmox_ssh_password
    mint_tailscale_key "${1:-}"
    ;;
  cloudflare)
    TF_VAR_cloudflare_api_token="$("$SECRETS" get "${CLOUDFLARE_TOKEN_KEY:-cloudflare/dns_api_token}")"
    export TF_VAR_cloudflare_api_token
    ;;
  *)
    die "unknown stack '${STACK}'"
    ;;
esac

STATE_URL="$("$SECRETS" get "$TF_STATE_URL_KEY")"
STATE_USER="$("$SECRETS" get "$TF_STATE_USER_KEY")"
STATE_PASSWORD="$("$SECRETS" get "$TF_STATE_PASSWORD_KEY")"
ADDRESS="${STATE_URL%/}/${STACK}/${WORKSPACE}"

echo "==> ${STACK}/${WORKSPACE}: init"
tofu -chdir="$STACK_DIR" init -input=false -reconfigure \
  -backend-config="address=${ADDRESS}" \
  -backend-config="lock_address=${ADDRESS}/lock" \
  -backend-config="unlock_address=${ADDRESS}/lock" \
  -backend-config="lock_method=POST" \
  -backend-config="unlock_method=DELETE" \
  -backend-config="retry_wait_min=5" \
  -backend-config="username=${STATE_USER}" \
  -backend-config="password=${STATE_PASSWORD}" \
  >/dev/null

declare -a ARGS=("$@")
SUBCOMMAND="${ARGS[0]}"

# plan/apply/destroy take variables; output/show/state do not.
case "$SUBCOMMAND" in
  plan|apply|destroy|refresh|import)
    if [[ -f "$VAR_FILE" ]]; then
      ARGS+=("-var-file=${VAR_FILE}")
    elif [[ "$STACK" != "cloudflare" ]]; then
      die "no var file at ${VAR_FILE} — run scripts/render_tfvars.py --stack ${WORKSPACE} first"
    fi
    ARGS+=("-input=false")
    ;;
esac

echo "==> ${STACK}/${WORKSPACE}: ${SUBCOMMAND}"
exec tofu -chdir="$STACK_DIR" "${ARGS[@]}"
