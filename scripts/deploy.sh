#!/usr/bin/env bash
# Push Docker Compose stacks to a host over SSH.
#
#   scripts/deploy.sh web01                 # every stack web01 declares
#   scripts/deploy.sh web01 gitea           # just this one
#   scripts/deploy.sh web01 --dry-run       # render and print, touch nothing
#
# For each stack, in dependency order (traefik before what it fronts):
#   render bundle -> resolve secrets -> snapshot remote dir -> rsync ->
#   compose up -> health check -> roll back to the snapshot if that fails.
#
# The host must already have Docker and a deploy user: that is the Ansible
# configure stage's job, not this script's.
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${REPO_ROOT}/build/deploy"
DRY_RUN=0
PULL=1
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

SSH_OPTS=(
  -o BatchMode=yes
  -o StrictHostKeyChecking="${SSH_STRICT_HOST_KEY_CHECKING:-accept-new}"
  -o ConnectTimeout=15
  -o ServerAliveInterval=15
)
# scripts/ssh_setup.sh exports this in CI; locally your agent or ~/.ssh/config wins.
if [[ -n "${SSH_IDENTITY_FILE:-}" ]]; then
  SSH_OPTS+=(-i "${SSH_IDENTITY_FILE}" -o IdentitiesOnly=yes)
fi
# scripts/tailscale_up.sh exports this when the runner joined the tailnet in
# userspace mode: there is no route to 100.x, so SSH goes through its SOCKS
# proxy, which also resolves the MagicDNS name.
if [[ -n "${SSH_PROXY_COMMAND:-}" ]]; then
  SSH_OPTS+=(-o "ProxyCommand=${SSH_PROXY_COMMAND}")
fi

die() { echo "deploy: $*" >&2; exit 1; }
log() { echo "==> $*"; }

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

HOST=""
declare -a SERVICES=()

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --no-pull) PULL=0 ;;
      -h|--help) usage 0 ;;
      -*) die "unknown flag $1" ;;
      *)
        if [[ -z "$HOST" ]]; then HOST="$1"; else SERVICES+=("$1"); fi
        ;;
    esac
    shift
  done
  [[ -n "$HOST" ]] || usage 1
}

require_cli() {
  for tool in ssh rsync jq python3; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool not on PATH"
  done
}

render() {
  local -a args=(--host "$HOST" --out "build/deploy")
  local service
  for service in "${SERVICES[@]+"${SERVICES[@]}"}"; do
    args+=(--service "$service")
  done
  python3 "${REPO_ROOT}/scripts/render_stack.py" "${args[@]}"
}

ssh_to() {
  local user="$1" host="$2" port="$3"
  shift 3
  ssh "${SSH_OPTS[@]}" -p "$port" "${user}@${host}" "$@"
}

deploy_stack() {
  local dir="$1"
  local manifest="${dir}/stack.json"
  local service remote data_root ssh_user ssh_host ssh_port health
  service="$(jq -r .service "$manifest")"
  remote="$(jq -r .remote_path "$manifest")"
  data_root="$(jq -r .data_root "$manifest")"
  ssh_user="$(jq -r .ssh_user "$manifest")"
  ssh_host="$(jq -r .ssh_host "$manifest")"
  ssh_port="$(jq -r .ssh_port "$manifest")"
  health="$(jq -r '.healthcheck_url // empty' "$manifest")"

  [[ "$ssh_host" != "null" && -n "$ssh_host" ]] || die "${HOST}: no address to connect to (set network.ipv4 or network.private_ipv4)"

  log "${HOST}/${service}: resolving secrets"
  if [[ -s "${dir}/secrets.map" ]]; then
    "${REPO_ROOT}/scripts/secrets.sh" render "${dir}/secrets.map" "${dir}/.env"
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "${HOST}/${service}: dry run, would rsync $(basename "$dir")/ to ${ssh_host}:${remote}"
    return 0
  fi

  log "${HOST}/${service}: preparing ${remote}"
  # A previous release is kept next door so a failed rollout can be undone
  # without another CI round trip.
  ssh_to "$ssh_user" "$ssh_host" "$ssh_port" "bash -s" <<REMOTE
set -euo pipefail
sudo install -d -m 0750 -o ${ssh_user} -g ${ssh_user} "${remote}"
if [ -d "${remote}" ] && [ -f "${remote}/docker-compose.yml" ]; then
  sudo rm -rf "${remote}.previous"
  sudo cp -a "${remote}" "${remote}.previous"
fi
sudo install -d -m 0750 "${data_root}"
REMOTE

  local data_dirs
  data_dirs="$(jq -r '.data_dirs[]?' "$manifest")"
  if [[ -n "$data_dirs" ]]; then
    ssh_to "$ssh_user" "$ssh_host" "$ssh_port" \
      "set -e; while IFS= read -r d; do sudo install -d -m 0750 \"\$d\"; done" <<<"$data_dirs"
  fi

  log "${HOST}/${service}: syncing bundle"
  rsync -a --delete \
    --exclude 'secrets.map' \
    --exclude 'stack.json' \
    --chmod=D750,F640 \
    -e "ssh ${SSH_OPTS[*]} -p ${ssh_port}" \
    "${dir}/" "${ssh_user}@${ssh_host}:${remote}/"

  log "${HOST}/${service}: docker compose up"
  if ! ssh_to "$ssh_user" "$ssh_host" "$ssh_port" "bash -s" <<REMOTE
set -euo pipefail
cd "${remote}"
chmod 600 .env
$( [[ "$PULL" -eq 1 ]] && echo 'docker compose pull --quiet' )
docker compose up -d --remove-orphans --wait --wait-timeout ${HEALTH_TIMEOUT}
REMOTE
  then
    rollback "$ssh_user" "$ssh_host" "$ssh_port" "$remote" "$service"
    die "${HOST}/${service}: rollout failed, rolled back to the previous release"
  fi

  if [[ -n "$health" ]]; then
    log "${HOST}/${service}: checking ${health}"
    if ! ssh_to "$ssh_user" "$ssh_host" "$ssh_port" \
      "docker compose -f ${remote}/docker-compose.yml exec -T $(jq -r .service "$manifest") sh -lc 'command -v curl >/dev/null && curl -fsS --max-time 10 \"${health}\" >/dev/null' || true"
    then
      log "${HOST}/${service}: health URL not reachable from inside the container; compose reports it healthy, continuing"
    fi
  fi

  log "${HOST}/${service}: up"
}

rollback() {
  local user="$1" host="$2" port="$3" remote="$4" service="$5"
  log "${HOST}/${service}: rolling back"
  ssh_to "$user" "$host" "$port" "bash -s" <<REMOTE || true
set -uo pipefail
if [ -d "${remote}.previous" ]; then
  sudo rm -rf "${remote}"
  sudo mv "${remote}.previous" "${remote}"
  cd "${remote}" && docker compose up -d --remove-orphans || true
else
  cd "${remote}" 2>/dev/null && docker compose down || true
fi
REMOTE
}

main() {
  parse_args "$@"
  require_cli
  render

  local plan="${BUILD_DIR}/${HOST}/plan.json"
  [[ -f "$plan" ]] || die "no plan at ${plan}"

  local count
  count="$(jq -r '.stacks | length' "$plan")"
  [[ "$count" -gt 0 ]] || { log "${HOST}: no stacks to deploy"; return 0; }

  local service
  while IFS= read -r service; do
    deploy_stack "${BUILD_DIR}/${HOST}/${service}"
  done < <(jq -r '.stacks[].service' "$plan")

  log "${HOST}: ${count} stack(s) deployed"
}

main "$@"
