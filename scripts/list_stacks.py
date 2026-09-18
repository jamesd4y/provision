#!/usr/bin/env python3
"""List the (stack, workspace) pairs a pipeline should act on, one per line.

Keeps inline Python out of the Woodpecker YAML, and gives the same answer on a
laptop as in CI.

  scripts/list_stacks.py --provisioned              # every cloud/proxmox folder
  scripts/list_stacks.py --changed build/changed.json
  scripts/list_stacks.py --changed build/changed.json --include-cloudflare
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import model


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provisioned", action="store_true", help="every enabled provisioning folder")
    parser.add_argument("--changed", help="scripts/changed.py output to narrow the list")
    parser.add_argument(
        "--include-cloudflare",
        action="store_true",
        help="also emit 'cloudflare default' when DNS is in scope",
    )
    args = parser.parse_args()
    if not args.provisioned and not args.changed:
        parser.error("pass --provisioned or --changed <file>")

    repo = model.load_repo()
    pairs: list[tuple[str, str]] = []
    cloudflare = False

    if args.provisioned:
        pairs = [
            (p.kind, name)
            for name, p in sorted(repo.providers.items())
            if p.provisioned and p.enabled
        ]
        cloudflare = bool(repo.zones)
    else:
        changed = json.loads(Path(args.changed).read_text(encoding="utf-8"))
        seen = {
            (repo.hosts[h].provider.kind, repo.hosts[h].provider.name)
            for h in changed.get("provisioned_hosts", [])
            if h in repo.hosts
        }
        pairs = sorted(seen)
        cloudflare = bool(changed.get("cloudflare")) and bool(repo.zones)

    for kind, name in pairs:
        print(f"{kind} {name}")
    if args.include_cloudflare and cloudflare:
        print("cloudflare default")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
