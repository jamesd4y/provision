"""Tailnet invariants.

Public SSH is gone, so the tailnet is the only way in. The mistakes worth
catching here are the ones that end with a host nobody can reach.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import render_tfvars  # noqa: E402
from lib import model  # noqa: E402


def test_every_host_has_a_way_in(repo):
    for host in repo.hosts.values():
        if not host.enabled:
            continue
        assert host.on_tailnet or host.public_ssh, (
            f"{host.name} is unreachable: no tailnet, no public SSH"
        )


def test_no_host_keeps_a_public_ssh_rule(repo):
    """The whole point of the tailnet here — nothing should still be listening."""
    for host in repo.hosts.values():
        assert not host.public_ssh, f"{host.name} still has public SSH enabled"
        ssh_rules = [r for r in host.network.get("firewall", []) if r["port"] == "22"]
        assert ssh_rules == [], f"{host.name} still declares a port 22 rule: {ssh_rules}"


def test_deploys_address_hosts_by_their_tailnet_name(repo):
    suffix = repo.tailnet["magic_dns_suffix"]
    for host in repo.hosts.values():
        if not host.enabled or not host.on_tailnet:
            continue
        assert host.ansible_host == f"{host.tailscale_hostname}.{suffix}", (
            f"{host.name} would be reached at {host.ansible_host}, not over the tailnet"
        )


def test_the_tailnet_role_runs_before_anything_that_needs_it(repo):
    """docker, firewall and the stacks all assume the host is already reachable."""
    for host in repo.hosts.values():
        if not host.enabled or not host.on_tailnet:
            continue
        roles = host.roles
        assert "tailscale" in roles, f"{host.name} joins the tailnet but never runs the role"
        position = roles.index("tailscale")
        for later in ("docker", "firewall"):
            if later in roles:
                assert position < roles.index(later), (
                    f"{host.name} runs {later} before joining the tailnet"
                )


def test_a_host_that_locks_down_ssh_also_runs_the_firewall_role(repo):
    for host in repo.hosts.values():
        if host.enabled and not host.public_ssh:
            assert "firewall" in host.roles, (
                f"{host.name} has no public SSH but never applies a ruleset"
            )


def test_direct_connection_port_is_opened_for_tailnet_hosts(repo):
    """Without it peers fall back to a DERP relay — it works, but slowly."""
    for host in repo.hosts.values():
        if not host.enabled:
            continue
        rules = render_tfvars._firewall_rules(host)
        udp = [r for r in rules if r["protocol"] == "udp" and r["port"] == "41641"]
        assert bool(udp) == host.on_tailnet, (
            f"{host.name}: tailscale UDP rule present={bool(udp)} but on_tailnet={host.on_tailnet}"
        )


def test_the_cloud_firewall_never_carries_ssh(repo):
    """What reaches the provider's firewall is what actually protects the host."""
    for host in repo.hosts.values():
        if not host.enabled:
            continue
        for rule in render_tfvars._firewall_rules(host):
            assert rule["port"] != "22", f"{host.name} sends a port 22 rule to its provider"


def test_tailnet_names_are_unique(repo):
    seen: dict[str, str] = {}
    for host in repo.hosts.values():
        if not host.enabled or not host.on_tailnet:
            continue
        name = host.tailscale_hostname
        assert name not in seen, f"{name} claimed by both {seen[name]} and {host.name}"
        seen[name] = host.name


def test_hosts_only_advertise_tags_the_oauth_client_owns(repo):
    known = set((repo.tailnet.get("tags") or {}).values())
    for host in repo.hosts.values():
        for tag in host.tailscale.get("tags") or []:
            assert tag in known, (
                f"{host.name} advertises {tag}, which tailscale/tailnet.yml does not declare"
            )


def test_the_tailnet_config_stores_keys_not_credentials(repo):
    for field in ("oauth_client_id", "oauth_client_secret"):
        value = repo.tailnet[field]
        assert not value.startswith("tskey-"), f"{field} looks like a real credential"
        assert model.SECRET_REF.match(value), f"{field} is not a valid Bitwarden key"


def test_cloud_init_only_registers_when_given_a_key(repo):
    """An empty key must produce a template with no tailscale block at all,
    rather than a `tailscale up --auth-key=` that fails at first boot."""
    template = (model.REPO_ROOT / "tofu" / "templates" / "cloud-init.yaml.tftpl").read_text()
    assert '%{ if tailscale_auth_key != "" ~}' in template
    assert "ephemeral=false" in template, "servers must not register as ephemeral nodes"
    assert "preauthorized=true" in template


def test_provisioning_stacks_ignore_cloud_init_churn(repo):
    """A fresh bootstrap key every apply must not recreate the fleet."""
    hetzner = (model.REPO_ROOT / "tofu" / "hetzner" / "main.tf").read_text()
    assert "ignore_changes = [user_data, ssh_keys]" in hetzner
    proxmox = (model.REPO_ROOT / "tofu" / "proxmox" / "main.tf").read_text()
    assert "ignore_changes = [source_raw]" in proxmox


def test_ci_registers_as_an_ephemeral_node(repo):
    """A CI runner that registers permanently leaves a dead device per pipeline."""
    script = (model.REPO_ROOT / "scripts" / "tailscale_up.sh").read_text()
    assert "ephemeral=true" in script
    assert "--advertise-tags=" in script
