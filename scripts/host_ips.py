#!/usr/bin/env python3
"""Collect the addresses OpenTofu assigned and cache them for later stages.

Provisioning runs before DNS and before Ansible, and a cloud host has no address
until its stack applies. Rather than couple the stacks together with remote
state, each apply writes its host_ips output here and the later stages read the
merged file.

  scripts/host_ips.py --from-tofu hetzner --workspace hetzner
  scripts/host_ips.py --from-tofu proxmox --workspace pve01
  scripts/host_ips.py --show

Addresses already declared in hosts/ (baremetal, private networks) are kept and
never overwritten by a null from a stack that does not know about them.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import model

OUT = model.REPO_ROOT / "build" / "host_ips.json"
FIELDS = ("ipv4", "ipv6", "private_ipv4")


def load() -> dict:
    if OUT.is_file():
        return json.loads(OUT.read_text(encoding="utf-8"))
    return {}


def save(data: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def merge(existing: dict, incoming: dict) -> dict:
    merged = json.loads(json.dumps(existing))
    for host, values in incoming.items():
        entry = merged.setdefault(host, {})
        for field in FIELDS:
            value = values.get(field)
            if value:
                entry[field] = value
    return merged


def from_tofu(stack: str, workspace: str) -> dict:
    """Read one stack's host_ips output through scripts/tofu.sh.

    The HTTP state backend has no named workspaces, so each (stack, workspace)
    pair is a separate state address; tofu.sh knows how to address them.
    """
    result = subprocess.run(
        [str(model.REPO_ROOT / "scripts" / "tofu.sh"), stack, workspace, "output", "-json", "host_ips"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # An unapplied stack has no outputs yet, which is normal during a
        # plan-only run; carry on with what hosts/ declares.
        print(
            f"host_ips: {stack}/{workspace} has no host_ips output yet "
            f"({result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'no output'})",
            file=sys.stderr,
        )
        return {}
    # tofu.sh prints progress on stdout before the JSON; take the last document.
    start = result.stdout.find("{")
    return json.loads(result.stdout[start:] or "{}") if start >= 0 else {}


def from_yaml() -> dict:
    repo = model.load_repo()
    return {
        host.name: {field: host.network.get(field) for field in FIELDS}
        for host in repo.hosts.values()
        if host.enabled
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-tofu", help="stack name: hetzner, proxmox or cloudflare")
    parser.add_argument("--workspace", help="the hosts/ folder this state belongs to")
    parser.add_argument("--show", action="store_true", help="print the merged file")
    parser.add_argument("--reset", action="store_true", help="start from what hosts/ declares")
    args = parser.parse_args()

    data = {} if args.reset else load()
    if args.reset or not data:
        data = merge(data, from_yaml())

    if args.from_tofu:
        if not args.workspace:
            parser.error("--from-tofu needs --workspace (the hosts/ folder name)")
        data = merge(data, from_tofu(args.from_tofu, args.workspace))

    save(data)
    if args.show or not args.from_tofu:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(f"host_ips: {len(data)} host(s) -> {OUT.relative_to(model.REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
