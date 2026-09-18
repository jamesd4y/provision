#!/usr/bin/env bash
# Bitwarden Secrets Manager access for the pipeline.
#
# The repo only ever stores secret KEYS (e.g. gitea/db_password). This resolves
# them to values at the last possible moment, into files created with mode 0600
# and shredded when the step ends.
#
#   secrets.sh get   gitea/db_password                      # one value on stdout
#   secrets.sh check build/.../secrets.map                  # do all keys resolve?
#   secrets.sh render build/.../secrets.map build/.../.env  # append VAR=value
#   secrets.sh export cloudflare/dns_api_token CF_API_TOKEN # eval "$(...)"
#
# Requires: bws (Bitwarden Secrets Manager CLI), jq, and BWS_ACCESS_TOKEN.
# Optional: BWS_PROJECT_ID scopes the lookup to one Secrets Manager project.
set -euo pipefail
umask 077

CACHE=""
MISSING_SENTINEL="__BWS_KEY_MISSING__"
AMBIGUOUS_SENTINEL="__BWS_KEY_AMBIGUOUS__"

cleanup() {
  if [[ -n "$CACHE" && -f "$CACHE" ]]; then
    shred -u "$CACHE" 2>/dev/null || rm -f "$CACHE"
  fi
  return 0
}
trap cleanup EXIT INT TERM

die() { echo "secrets: $*" >&2; exit 1; }

require_cli() {
  command -v bws >/dev/null 2>&1 || die "bws not on PATH - install the Bitwarden Secrets Manager CLI"
  command -v jq >/dev/null 2>&1 || die "jq not on PATH"
  [[ -n "${BWS_ACCESS_TOKEN:-}" ]] || die "BWS_ACCESS_TOKEN is not set (Woodpecker secret 'bws_access_token')"
}

# One list call per invocation; every lookup reads the cache. That keeps a
# pipeline resolving dozens of keys well inside the API rate limit.
load_cache() {
  [[ -n "$CACHE" ]] && return 0
  require_cli
  CACHE="$(mktemp -t bws-secrets.XXXXXX)"
  local -a args=(secret list --output json)
  if [[ -n "${BWS_PROJECT_ID:-}" ]]; then
    args=(secret list "${BWS_PROJECT_ID}" --output json)
  fi
  local errfile
  errfile="$(mktemp -t bws-err.XXXXXX)"
  if ! bws "${args[@]}" >"$CACHE" 2>"$errfile"; then
    local err
    err="$(cat "$errfile")"
    rm -f "$errfile"
    die "bws secret list failed: ${err}"
  fi
  rm -f "$errfile"
}

lookup() {
  local key="$1" value
  load_cache
  value="$(jq -r --arg k "$key" --arg missing "$MISSING_SENTINEL" --arg ambiguous "$AMBIGUOUS_SENTINEL" \
    "[.[] | select(.key == \$k)] | if length == 0 then \$missing elif length > 1 then \$ambiguous else .[0].value end" \
    "$CACHE")"
  if [[ "$value" == "$MISSING_SENTINEL" ]]; then
    die "no secret with key '${key}' - check BWS_PROJECT_ID and the machine account's access"
  fi
  if [[ "$value" == "$AMBIGUOUS_SENTINEL" ]]; then
    die "more than one secret has key '${key}' - keys must be unique within the project"
  fi
  printf '%s' "$value"
}

# secrets.map lines are "ENV_VAR=secret/key"; comments and blanks are skipped.
each_mapping() {
  local map="$1" callback="$2" line var key
  [[ -f "$map" ]] || die "no such secrets map: $map"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%[[:space:]]}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == *=* ]] || die "malformed line in ${map}: ${line}"
    var="${line%%=*}"
    key="${line#*=}"
    [[ -n "$var" && -n "$key" ]] || die "malformed line in ${map}: ${line}"
    "$callback" "$var" "$key"
  done <"$map"
}

cmd_get() {
  [[ $# -eq 1 ]] || die "usage: secrets.sh get <key>"
  lookup "$1"
  echo
}

CHECKED=0
_check_one() {
  lookup "$2" >/dev/null
  echo "  ok  $1 <- $2"
  CHECKED=$((CHECKED + 1))
}

cmd_check() {
  [[ $# -eq 1 ]] || die "usage: secrets.sh check <secrets.map>"
  each_mapping "$1" _check_one
  echo "secrets: ${CHECKED} key(s) resolve"
}

RENDER_TARGET=""
RENDERED=0
_render_one() {
  local value
  value="$(lookup "$2")"
  # A newline in a value would end the .env line early and could smuggle in
  # another variable, so refuse it rather than silently mangling the file.
  if [[ "$(printf '%s' "$value" | wc -l)" -gt 0 ]]; then
    die "secret '$2' contains a newline, which .env cannot represent"
  fi
  printf '%s=%s\n' "$1" "$value" >>"$RENDER_TARGET"
  RENDERED=$((RENDERED + 1))
}

cmd_render() {
  [[ $# -eq 2 ]] || die "usage: secrets.sh render <secrets.map> <target.env>"
  RENDER_TARGET="$2"
  : >>"$RENDER_TARGET"
  chmod 600 "$RENDER_TARGET"
  each_mapping "$1" _render_one
  echo "secrets: rendered ${RENDERED} value(s) into ${RENDER_TARGET}"
}

cmd_export() {
  [[ $# -ge 1 ]] || die "usage: secrets.sh export <key> [VAR_NAME]"
  local key="$1" var
  var="${2:-$(printf '%s' "${key##*/}" | tr '[:lower:]' '[:upper:]' | tr '.-' '__')}"
  printf 'export %s=%q\n' "$var" "$(lookup "$key")"
}

main() {
  [[ $# -ge 1 ]] || die "usage: secrets.sh {get|check|render|export} ..."
  local command="$1"
  shift
  case "$command" in
    get) cmd_get "$@" ;;
    check) cmd_check "$@" ;;
    render) cmd_render "$@" ;;
    export) cmd_export "$@" ;;
    *) die "unknown command '${command}'" ;;
  esac
}

main "$@"
