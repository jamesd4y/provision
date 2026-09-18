#!/usr/bin/env bash
# Put the CI SSH identity in place for the configure and deploy stages.
#
# The private key never touches the repo or a Woodpecker secret: it lives in
# Bitwarden Secrets Manager and is written here, mode 0600, into the ephemeral
# container's home directory.
#
#   eval "$(scripts/ssh_setup.sh)"      # exports SSH_* for later steps
#
# Keys used (override with the matching *_KEY env var):
#   ssh/ci_private_key   the deploy identity
#   ssh/known_hosts      pinned host keys; optional but strongly preferred
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS="${REPO_ROOT}/scripts/secrets.sh"

: "${SSH_PRIVATE_KEY_KEY:=ssh/ci_private_key}"
: "${SSH_KNOWN_HOSTS_KEY:=ssh/known_hosts}"

SSH_DIR="${HOME}/.ssh"
mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"

KEY_FILE="${SSH_DIR}/id_provision"
"$SECRETS" get "$SSH_PRIVATE_KEY_KEY" >"$KEY_FILE"
chmod 600 "$KEY_FILE"
# `secrets.sh get` appends a newline; OpenSSH needs the key to end with exactly one.
printf '%s\n' "$(cat "$KEY_FILE")" >"$KEY_FILE.tmp" && mv "$KEY_FILE.tmp" "$KEY_FILE"
chmod 600 "$KEY_FILE"

KNOWN_HOSTS="${SSH_DIR}/known_hosts"
if "$SECRETS" get "$SSH_KNOWN_HOSTS_KEY" >"$KNOWN_HOSTS" 2>/dev/null && [ -s "$KNOWN_HOSTS" ]; then
  chmod 600 "$KNOWN_HOSTS"
  STRICT=yes
  echo "ssh_setup: pinned $(grep -c . "$KNOWN_HOSTS") host key(s)" >&2
else
  rm -f "$KNOWN_HOSTS"
  STRICT=accept-new
  echo "ssh_setup: no ${SSH_KNOWN_HOSTS_KEY} secret; trusting host keys on first use" >&2
  echo "ssh_setup: add one to pin them - see docs/secrets.md" >&2
fi

cat <<ENV
export SSH_IDENTITY_FILE='${KEY_FILE}'
export ANSIBLE_PRIVATE_KEY_FILE='${KEY_FILE}'
export ANSIBLE_HOST_KEY_CHECKING='$([ "$STRICT" = yes ] && echo True || echo False)'
export SSH_STRICT_HOST_KEY_CHECKING='${STRICT}'
export GIT_SSH_COMMAND='ssh -i ${KEY_FILE} -o StrictHostKeyChecking=${STRICT}'
ENV
