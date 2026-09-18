"""MicroOS host generation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import render_ignition  # noqa: E402
import render_tfvars  # noqa: E402
from lib import model  # noqa: E402


def test_microos_hosts_are_recognised(repo):
    assert repo.hosts["edge01"].is_microos
    assert not repo.hosts["web01"].is_microos


def test_ignition_is_valid_shape(repo):
    config = render_ignition.render_ignition(repo, repo.hosts["edge01"], "tskey-test")
    assert config["ignition"]["version"] == "3.4.0"
    user = config["passwd"]["users"][0]
    assert user["name"] == "deploy"
    assert user["sshAuthorizedKeys"], "a host with no key on it cannot be reached"
    paths = {f["path"] for f in config["storage"]["files"]}
    assert "/etc/ssh/sshd_config.d/10-provision.conf" in paths
    assert "/usr/local/bin/provision-bootstrap" in paths


def test_ignition_never_leaves_a_password_login(repo):
    config = render_ignition.render_ignition(repo, repo.hosts["edge01"], "")
    import urllib.parse
    sshd = next(f for f in config["storage"]["files"] if f["path"].endswith("10-provision.conf"))
    body = urllib.parse.unquote(sshd["contents"]["source"].removeprefix("data:,"))
    assert "PasswordAuthentication no" in body
    assert "PermitRootLogin no" in body


def test_the_bootstrap_installs_what_the_repo_needs(repo):
    config = render_ignition.render_ignition(repo, repo.hosts["edge01"], "tskey-test")
    import urllib.parse
    script = next(f for f in config["storage"]["files"] if f["path"].endswith("provision-bootstrap"))
    body = urllib.parse.unquote(script["contents"]["source"].removeprefix("data:,"))
    for package in ("docker", "tailscale"):
        assert package in body
    # transactional-update needs a reboot before a package is usable; the
    # bootstrap must handle that rather than assuming the install took effect.
    assert "systemctl reboot" in body
    assert "tailscale up" in body


def test_combustion_runs_inside_transactional_update(repo):
    script = render_ignition.combustion_script(repo.hosts["edge01"], repo.tailnet, "tskey-test")
    assert script.startswith("#!/bin/bash")
    # The magic comment is what makes networking available to the script.
    assert "# combustion: network" in script
    assert "zypper" in script


def test_tfvars_carry_ignition_only_for_microos_hosts(repo):
    render_tfvars._REPO.clear()
    render_tfvars._REPO.append(repo)
    data = render_tfvars.render_provider_folder(repo, "pve01")
    assert data["hosts"]["edge01"]["ignition"] != ""
    assert data["hosts"]["media01"]["ignition"] == ""
    config = json.loads(data["hosts"]["edge01"]["ignition"])
    assert config["ignition"]["version"] == "3.4.0"


def test_the_bootstrap_key_is_a_placeholder_in_generated_files(repo):
    """The real key is substituted by OpenTofu at apply time, so it is never
    written to a file this repo produces."""
    render_tfvars._REPO.clear()
    render_tfvars._REPO.append(repo)
    data = render_tfvars.render_provider_folder(repo, "pve01")
    ignition = data["hosts"]["edge01"]["ignition"]
    assert render_ignition.AUTH_KEY_PLACEHOLDER in ignition
    assert "tskey-" not in ignition


def test_microos_hosts_still_join_the_tailnet(repo):
    host = repo.hosts["edge01"]
    assert host.on_tailnet
    assert host.ansible_host.endswith(repo.tailnet["magic_dns_suffix"])
