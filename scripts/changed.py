#!/usr/bin/env python3
"""Work out the smallest correct blast radius for a change.

Woodpecker calls this at the top of a pipeline so a one-line edit to one service
does not redeploy the fleet. It errs towards doing too much: anything shared
(scripts/, schemas/, the base role) expands to everything.

  scripts/changed.py --base origin/main            # JSON summary
  scripts/changed.py --base origin/main --field hosts --format lines
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import model
from lib.model import ModelError, Repo

# Touching any of these means we cannot reason about the blast radius: do it all.
GLOBAL_PATHS = ("scripts/", "schemas/", "requirements.txt", "ansible/ansible.cfg",
                "ansible/group_vars/", "ansible/site.yml", "ansible/check.yml")


def git_diff(base: str, head: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...{head}"],
            cwd=model.REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        # No common ancestor (shallow clone, first push): fall back to a plain diff.
        out = subprocess.run(
            ["git", "diff", "--name-only", base, head],
            cwd=model.REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    return [line for line in out.stdout.splitlines() if line.strip()]


def analyse(repo: Repo, paths: list[str]) -> dict:
    everything = any(p.startswith(GLOBAL_PATHS) for p in paths)

    hosts: set[str] = set()
    services: set[str] = set()
    providers: set[str] = set()
    roles: set[str] = set()
    tofu_stacks: set[str] = set()
    cloudflare = False

    for path in paths:
        parts = Path(path).parts
        if path.startswith("hosts/") and len(parts) == 3:
            provider, filename = parts[1], parts[2]
            if filename == model.PROVIDER_CONFIG_NAME:
                providers.add(provider)
            elif filename.endswith(".yml"):
                hosts.add(filename[: -len(".yml")])
        elif path.startswith("services/") and path.endswith(".yml") and len(parts) == 2:
            services.add(parts[1][: -len(".yml")])
        elif path.startswith("cloudflare/"):
            cloudflare = True
        elif path.startswith("tofu/") and len(parts) >= 2:
            tofu_stacks.add(parts[1])
        elif path.startswith("ansible/roles/") and len(parts) >= 3:
            roles.add(parts[2])

    if everything:
        hosts |= {h.name for h in repo.hosts.values() if h.enabled}
        services |= set(repo.services)
        providers |= set(repo.providers)
        cloudflare = True

    for provider in providers:
        hosts |= {h.name for h in repo.hosts_for_provider(provider)}
    for role in roles:
        hosts |= {h.name for h in repo.hosts.values() if h.enabled and role in h.roles}
    for service in services:
        hosts |= {h.name for h in repo.hosts_running(service)}

    hosts = {name for name in hosts if name in repo.hosts and repo.hosts[name].enabled}

    # A host change can move a service or a domain, so its DNS has to be re-checked.
    if hosts:
        cloudflare = True

    # (host, service) pairs that actually need a compose rollout.
    deployments = []
    for host_name in sorted(hosts):
        host = repo.hosts[host_name]
        host_file_changed = str(host.path.relative_to(model.REPO_ROOT)) in paths or everything
        for binding in host.service_bindings:
            if host_file_changed or binding["name"] in services:
                deployments.append({"host": host_name, "service": binding["name"]})

    provisioned = sorted(
        {h for h in hosts if repo.hosts[h].provider.provisioned}
    )
    for host_name in provisioned:
        tofu_stacks.add(repo.hosts[host_name].provider.kind)
    if cloudflare:
        tofu_stacks.add("cloudflare")

    return {
        "everything": everything,
        "paths": paths,
        "hosts": sorted(hosts),
        "provisioned_hosts": provisioned,
        "services": sorted(services),
        "providers": sorted(providers),
        "roles": sorted(roles),
        "tofu_stacks": sorted(tofu_stacks),
        "cloudflare": cloudflare,
        "deployments": deployments,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="commit to diff against")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--field", help="print one field instead of the whole summary")
    parser.add_argument("--format", choices=["json", "lines"], default="json")
    parser.add_argument(
        "--all", action="store_true", help="ignore git and treat everything as changed"
    )
    args = parser.parse_args()

    repo = model.load_repo()
    paths = [] if args.all else git_diff(args.base, args.head)
    result = analyse(repo, paths)
    if args.all:
        result = analyse(repo, ["scripts/"])
        result["paths"] = []

    value = result[args.field] if args.field else result
    if args.format == "lines" and isinstance(value, list):
        for item in value:
            print(item if isinstance(item, str) else json.dumps(item))
    else:
        print(json.dumps(value, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
