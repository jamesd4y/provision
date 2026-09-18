#!/usr/bin/env python3
"""Ansible dynamic inventory built from hosts/.

There is no inventory file to keep in sync — the host YAML is the inventory.

  ansible-inventory -i scripts/inventory.py --list
  ansible-playbook  -i scripts/inventory.py ansible/playbooks/site.yml --limit web01

Groups produced:
  provider_<folder>   every host in hosts/<folder>/
  kind_<kind>         hetzner | proxmox | baremetal
  role_<role>         hosts applying that Ansible role
  service_<stack>     hosts running that stack
  tag_<tag>           hosts carrying that tag
  provisioned         hosts OpenTofu creates (i.e. not baremetal)

Disabled hosts and disabled providers are left out entirely, which is how you
take a machine out of rotation without deleting its file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import model
from lib.model import Host, ModelError, Repo


def hostvars(repo: Repo, host: Host) -> dict:
    bindings = []
    for binding in host.service_bindings:
        service = repo.services.get(binding["name"])
        bindings.append(
            {
                "name": binding["name"],
                "domains": [d["name"] for d in binding.get("domains") or []],
                "data_root": binding.get("volumes_root") or f"/srv/{binding['name']}",
                "data_dirs": service.data_dirs if service else [],
                "ingress": service.ingress if service else "none",
            }
        )

    tailscale = host.tailscale
    default_tags = [((repo.tailnet.get("tags") or {}).get("server") or "tag:server")]

    return {
        "ansible_host": host.ansible_host,
        "ansible_user": host.ansible.get("user", "deploy"),
        "ansible_port": host.ansible.get("port", 22),
        "ansible_python_interpreter": host.ansible.get("python", "/usr/bin/python3"),
        "provision_provider": host.provider.name,
        "provision_kind": host.provider.kind,
        "provision_fqdn": host.fqdn,
        "provision_description": host.raw.get("description", ""),
        "provision_roles": host.roles,
        "provision_tags": host.tags,
        "provision_services": bindings,
        # Declared hardware. The base role compares it against gathered facts and
        # fails loudly when a baremetal box does not match its own description.
        "provision_declared_cpu": host.hardware.get("cpu"),
        "provision_declared_memory_mb": host.hardware.get("memory"),
        "provision_storage": host.hardware.get("storage", []),
        "provision_firewall": host.network.get("firewall", []),
        "provision_public_ssh": host.public_ssh,
        # Tailscale. The role reads these; the firewall role uses the enabled
        # flag to decide whether the tailnet counts as a trusted interface.
        "tailscale_enabled": host.on_tailnet,
        "tailscale_node_name": host.tailscale_hostname,
        "tailscale_tags": tailscale.get("tags") or default_tags,
        "tailscale_accept_dns": tailscale.get("accept_dns", True),
        "tailscale_accept_routes": tailscale.get("accept_routes", False),
        "tailscale_ssh": tailscale.get("ssh", False),
        "tailscale_extra_args": tailscale.get("extra_args", []),
        "provision_timezone": host.os.get("timezone", "UTC"),
        "provision_os": host.os,
        # MicroOS has no apt and no mutable /usr: packages arrive via Ignition
        # and Combustion at first boot, so the roles skip installation there
        # and manage configuration and services only.
        "provision_microos": host.is_microos,
        # Backups. The plan is computed from the stacks this host runs, so the
        # role renders it rather than deciding it.
        "provision_backup": model.backup_plan(repo, host),
        "provision_backup_repository": (
            (repo.backup.get("repository") or {}).get("url", "").format(host=host.name)
            if repo.backup else ""
        ),
        "provision_backup_staging": model.BACKUP_STAGING_ROOT,
        **(host.ansible.get("vars") or {}),
    }


def build(repo: Repo) -> dict:
    inventory: dict = {
        "_meta": {"hostvars": {}},
        "all": {"children": ["ungrouped"]},
    }

    def group(name: str) -> dict:
        safe = name.replace("-", "_").replace(".", "_")
        entry = inventory.setdefault(safe, {"hosts": []})
        if safe not in inventory["all"]["children"]:
            inventory["all"]["children"].append(safe)
        return entry

    for host in sorted(repo.hosts.values(), key=lambda h: h.name):
        if not host.enabled:
            continue
        inventory["_meta"]["hostvars"][host.name] = hostvars(repo, host)
        group(f"provider_{host.provider.name}")["hosts"].append(host.name)
        group(f"kind_{host.provider.kind}")["hosts"].append(host.name)
        if host.provider.provisioned:
            group("provisioned")["hosts"].append(host.name)
        for role in host.roles:
            group(f"role_{role}")["hosts"].append(host.name)
        for binding in host.service_bindings:
            group(f"service_{binding['name']}")["hosts"].append(host.name)
        for tag in host.tags:
            group(f"tag_{tag}")["hosts"].append(host.name)

    return inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="emit the whole inventory")
    parser.add_argument("--host", help="emit one host's vars (Ansible compatibility)")
    args = parser.parse_args()

    repo = model.load_repo()
    if args.host:
        host = repo.hosts.get(args.host)
        print(json.dumps(hostvars(repo, host) if host else {}, indent=2, sort_keys=True))
        return 0

    print(json.dumps(build(repo), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
