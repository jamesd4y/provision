#!/usr/bin/env bash
# Rebuild build/host_ips.json from every provisioned stack.
#
# Woodpecker gives each workflow its own clone, so nothing carries over from the
# provision pipeline to the DNS, configure and deploy pipelines. Rather than
# pass an artifact between them, each stage reconstructs the addresses from
# state, which is the authoritative copy anyway.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Start from what hosts/ declares: baremetal addresses and private networks are
# written down rather than discovered.
python3 scripts/host_ips.py --reset >/dev/null

while read -r stack workspace; do
  [ -z "${stack:-}" ] && continue
  echo "==> reading addresses from ${stack}/${workspace}"
  python3 scripts/host_ips.py --from-tofu "$stack" --workspace "$workspace" || true
done < <(python3 scripts/list_stacks.py --provisioned)

python3 scripts/host_ips.py --show
