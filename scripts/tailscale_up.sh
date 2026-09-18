#!/usr/bin/env bash
# Join the tailnet from a CI step, so the pipeline can reach hosts that have no
# public SSH at all.
#
#   eval "$(scripts/tailscale_up.sh)"
#
# Prints shell exports on stdout and progress on stderr, so it composes with
# eval the same way scripts/ssh_setup.sh does.
#
# The node is ephemeral and tagged tag:ci: Tailscale removes it shortly after
# tailscaled exits, so a fleet's device list does not fill up with dead runners.
#
# Two networking modes, picked automatically:
#
#   kernel     /dev/net/tun exists and we can use it. Normal routing; MagicDNS
#              names resolve through the system resolver; nothing else to do.
#   userspace  no TUN device, which is the usual case for an unprivileged
#              container. tailscaled runs a SOCKS5 proxy instead, and SSH is
#              pointed through it. DNS is resolved by the proxy, so MagicDNS
#              names still work.
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAILNET_FILE="${REPO_ROOT}/tailscale/tailnet.yml"

SOCKS_PORT="${TS_SOCKS_PORT:-1055}"
STATE_DIR="${TS_STATE_DIR:-/var/lib/tailscale-ci}"
SOCKET="${TS_SOCKET:-/var/run/tailscale-ci.sock}"
TIMEOUT="${TS_TIMEOUT:-90}"

log() { echo "tailscale_up: $*" >&2; }
die() { log "$*"; exit 1; }

command -v tailscaled >/dev/null 2>&1 || die "tailscaled not on PATH (add it to ci/Dockerfile)"
command -v tailscale >/dev/null 2>&1 || die "tailscale not on PATH"
[[ -f "$TAILNET_FILE" ]] || die "no tailscale/tailnet.yml — the tailnet is not configured"

CI_TAG="$(python3 -c "
import yaml
config = yaml.safe_load(open('${TAILNET_FILE}'))
print((config.get('tags') or {}).get('ci') or 'tag:ci')
")"

# Already up from an earlier step in this workflow? Reuse it.
if tailscale --socket="$SOCKET" status --json >/dev/null 2>&1; then
  state="$(tailscale --socket="$SOCKET" status --json | jq -r .BackendState)"
  if [[ "$state" == "Running" ]]; then
    log "already on the tailnet, reusing the existing session"
    emit_only=true
  fi
fi

if [[ "${emit_only:-false}" != true ]]; then
  mkdir -p "$STATE_DIR"

  if [[ -c /dev/net/tun ]]; then
    MODE=kernel
    log "found /dev/net/tun, using kernel networking"
    tailscaled \
      --statedir="$STATE_DIR" \
      --socket="$SOCKET" \
      >"${STATE_DIR}/tailscaled.log" 2>&1 &
  else
    MODE=userspace
    log "no TUN device, using userspace networking with a SOCKS5 proxy on ${SOCKS_PORT}"
    tailscaled \
      --tun=userspace-networking \
      --socks5-server="127.0.0.1:${SOCKS_PORT}" \
      --outbound-http-proxy-listen="127.0.0.1:${SOCKS_PORT}" \
      --statedir="$STATE_DIR" \
      --socket="$SOCKET" \
      >"${STATE_DIR}/tailscaled.log" 2>&1 &
  fi
  echo "$!" >"${STATE_DIR}/tailscaled.pid"

  for _ in $(seq 1 30); do
    tailscale --socket="$SOCKET" status >/dev/null 2>&1 && break
    sleep 1
  done

  AUTH_KEY="$("${REPO_ROOT}/scripts/tailscale_authkey.sh" --tag "$CI_TAG" --ephemeral)"
  log "registering as an ephemeral ${CI_TAG} node"
  tailscale --socket="$SOCKET" up \
    --auth-key="${AUTH_KEY}?ephemeral=true&preauthorized=true" \
    --hostname="${TS_HOSTNAME:-woodpecker-${CI_PIPELINE_NUMBER:-local}}" \
    --advertise-tags="$CI_TAG" \
    --accept-dns=true \
    --accept-routes=true \
    --timeout="${TIMEOUT}s"
  unset AUTH_KEY
else
  MODE="${TS_MODE:-userspace}"
  [[ -c /dev/net/tun ]] && MODE=kernel
fi

state="$(tailscale --socket="$SOCKET" status --json | jq -r .BackendState)"
[[ "$state" == "Running" ]] || die "tailscaled came up in state '${state}', not Running"
log "on the tailnet as $(tailscale --socket="$SOCKET" status --json | jq -r '.Self.DNSName // .Self.HostName')"

cat <<ENV
export TS_SOCKET='${SOCKET}'
export TS_MODE='${MODE}'
ENV

if [[ "$MODE" == userspace ]]; then
  # nc -X 5 hands the hostname to the proxy, so MagicDNS names are resolved on
  # the tailnet side rather than by this container's resolver.
  proxy_command="nc -X 5 -x 127.0.0.1:${SOCKS_PORT} %h %p"
  cat <<ENV
export ALL_PROXY='socks5://127.0.0.1:${SOCKS_PORT}'
export SSH_PROXY_COMMAND='${proxy_command}'
export ANSIBLE_SSH_COMMON_ARGS='-o ProxyCommand="${proxy_command}"'
ENV
fi
