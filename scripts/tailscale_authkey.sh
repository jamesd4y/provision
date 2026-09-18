#!/usr/bin/env bash
# Mint a short-lived Tailscale auth key from the OAuth client.
#
# Hosts are registered with a key, not with the OAuth client secret itself: a
# key can be scoped to one tag, given an hour to live, and is worthless once it
# expires — which matters because this key travels in cloud-init user data,
# and user data is readable by anyone holding the provider's API token.
#
#   scripts/tailscale_authkey.sh                 # tag:server, reusable, 1h
#   scripts/tailscale_authkey.sh --tag tag:ci --ephemeral
#
# Reads tailscale/tailnet.yml for the OAuth credential KEYS and resolves them
# through scripts/secrets.sh. Prints the key on stdout and nothing else, so it
# is safe to capture in a variable.
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS="${REPO_ROOT}/scripts/secrets.sh"
TAILNET_FILE="${REPO_ROOT}/tailscale/tailnet.yml"

TAG=""
EPHEMERAL=false
REUSABLE=true
EXPIRY=""

die() { echo "tailscale_authkey: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) TAG="${2:-}"; shift 2 ;;
    --ephemeral) EPHEMERAL=true; shift ;;
    --single-use) REUSABLE=false; shift ;;
    --expiry) EXPIRY="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument $1" ;;
  esac
done

for tool in curl jq python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not on PATH"
done
[[ -f "$TAILNET_FILE" ]] || die "no tailscale/tailnet.yml — the tailnet is not configured"

read_tailnet() {
  python3 -c "
import sys, yaml
config = yaml.safe_load(open('${TAILNET_FILE}'))
value = config
for part in sys.argv[1].split('.'):
    value = (value or {}).get(part)
print(value if value is not None else '')
" "$1"
}

CLIENT_ID_KEY="$(read_tailnet oauth_client_id)"
CLIENT_SECRET_KEY="$(read_tailnet oauth_client_secret)"
[[ -n "$CLIENT_ID_KEY" && -n "$CLIENT_SECRET_KEY" ]] || die "tailnet.yml has no OAuth credential keys"

if [[ -z "$TAG" ]]; then
  TAG="$(read_tailnet tags.server)"
  [[ -n "$TAG" ]] || die "no tags.server in tailnet.yml and no --tag given"
fi
if [[ -z "$EXPIRY" ]]; then
  EXPIRY="$(read_tailnet authkey_expiry_seconds)"
  [[ -n "$EXPIRY" ]] || EXPIRY=3600
fi

CLIENT_ID="$("$SECRETS" get "$CLIENT_ID_KEY")"
CLIENT_SECRET="$("$SECRETS" get "$CLIENT_SECRET_KEY")"

# An OAuth access token is valid for one hour and cannot be extended, which is
# fine: it is used once, immediately, to mint the key.
TOKEN="$(curl -fsS --max-time 30 \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  https://api.tailscale.com/api/v2/oauth/token \
  | jq -re .access_token)" || die "could not get an OAuth access token — check the client id/secret and that the client has the auth_keys scope"

BODY="$(jq -nc \
  --argjson reusable "$REUSABLE" \
  --argjson ephemeral "$EPHEMERAL" \
  --argjson expiry "$EXPIRY" \
  --arg tag "$TAG" \
  '{
     description: "provision bootstrap",
     expirySeconds: $expiry,
     capabilities: {
       devices: {
         create: {
           reusable: $reusable,
           ephemeral: $ephemeral,
           preauthorized: true,
           tags: [$tag]
         }
       }
     }
   }')"

# "-" is the shorthand for "the tailnet this credential belongs to".
RESPONSE="$(curl -fsS --max-time 30 \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  --data "$BODY" \
  https://api.tailscale.com/api/v2/tailnet/-/keys)" \
  || die "could not create an auth key — does the OAuth client own ${TAG}?"

KEY="$(printf '%s' "$RESPONSE" | jq -re .key)" \
  || die "unexpected response from the keys API"

printf '%s\n' "$KEY"
