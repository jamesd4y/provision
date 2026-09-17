"""What the generators produce, and what must never appear in it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import changed as change_analysis  # noqa: E402
import render_stack  # noqa: E402
import render_tfvars  # noqa: E402
from lib import model  # noqa: E402


def test_stacks_render_in_dependency_order(repo, tmp_path):
    host = repo.hosts["web01"]
    wanted = [b["name"] for b in host.service_bindings]
    order = render_stack.order_services(repo, host, wanted)
    # gitea and uptime-kuma both require traefik, so traefik precedes both.
    assert order.index("traefik") < order.index("gitea")
    assert order.index("traefik") < order.index("uptime-kuma")
    assert set(order) == set(wanted)


def test_bundle_keeps_secret_values_out(repo, tmp_path):
    host = repo.hosts["web01"]
    manifest = render_stack.render_one(repo, host, repo.services["gitea"], tmp_path)
    bundle = tmp_path / "web01" / "gitea"

    env = (bundle / ".env").read_text()
    assert "GITEA_DB_PASSWORD" not in env  # resolved at deploy time, not here

    secrets_map = (bundle / "secrets.map").read_text()
    assert "GITEA_DB_PASSWORD=gitea/db_password" in secrets_map

    assert manifest["secret_vars"] == ["GITEA_DB_PASSWORD", "GITEA__mailer__PASSWD"]
    assert manifest["remote_path"] == "/opt/stacks/gitea"


def test_override_carries_the_generated_labels(repo, tmp_path):
    render_stack.render_one(repo, repo.hosts["web01"], repo.services["gitea"], tmp_path)
    override = (tmp_path / "web01" / "gitea" / "docker-compose.override.yml").read_text()
    assert "Host(`git.example.com`)" in override
    assert "edge" in override


def test_internal_stack_gets_no_override(repo, tmp_path):
    render_stack.render_one(repo, repo.hosts["db01"], repo.services["postgres"], tmp_path)
    assert not (tmp_path / "db01" / "postgres" / "docker-compose.override.yml").exists()


def test_cloudflare_records_follow_the_host_that_runs_the_service(repo, monkeypatch):
    monkeypatch.setattr(
        render_tfvars,
        "collect_host_ips",
        lambda _repo: {"web01": {"ipv4": "203.0.113.10"}, "media01": {"ipv4": "198.51.100.30"}},
    )
    data = render_tfvars.render_cloudflare(repo)
    records = data["records"]

    git = records["service/web01/gitea/git.example.com/A"]
    assert git == {
        "zone": "example.com",
        "name": "git",
        "type": "A",
        "value": "203.0.113.10",
        "ttl": 1,
        "priority": None,
        "proxied": True,
        "comment": "managed-by:provision host=web01 service=gitea",
    }
    # The status page on the Proxmox box points at that box, not at web01.
    assert records["service/media01/uptime-kuma/home-status.example.com/A"]["value"] == "198.51.100.30"
    # Host records stay grey-clouded so SSH and DNS-01 keep working.
    assert records["host/web01/A"]["proxied"] is False


def test_records_for_hosts_without_an_address_are_skipped(repo, monkeypatch):
    monkeypatch.setattr(render_tfvars, "collect_host_ips", lambda _repo: {})
    records = render_tfvars.render_cloudflare(repo)["records"]
    assert not any(key.startswith("service/") for key in records)
    # Zone-level records do not depend on any host, so they survive.
    assert any(key.startswith("zone/example.com") for key in records)


def test_tfvars_carry_secret_keys_never_values(repo):
    data = render_tfvars.render_provider_folder(repo, "hetzner")
    assert data["credentials"] == {"hcloud_token": "hetzner/api_token"}
    assert "web01" in data["hosts"]
    assert data["hosts"]["web01"]["cpu"] == 2
    assert data["hosts"]["web01"]["memory_mb"] == 4096


def test_extra_disks_are_separated_from_the_root_disk(repo):
    web01 = render_tfvars.render_provider_folder(repo, "hetzner")["hosts"]["web01"]
    assert web01["root_disk_gb"] == 40
    assert [d["name"] for d in web01["extra_disks"]] == ["data"]
    assert web01["extra_disks"][0]["mount"] == "/srv"


def test_changing_one_service_only_redeploys_the_hosts_running_it(repo):
    result = change_analysis.analyse(repo, ["services/gitea.yml"])
    assert result["hosts"] == ["web01"]
    assert result["deployments"] == [{"host": "web01", "service": "gitea"}]


def test_changing_a_host_file_redeploys_all_of_its_stacks(repo):
    result = change_analysis.analyse(repo, ["hosts/hetzner/web01.yml"])
    assert {d["service"] for d in result["deployments"]} == {
        "vector", "traefik", "gitea", "uptime-kuma",
    }
    assert result["tofu_stacks"] == ["cloudflare", "hetzner"]


def test_changing_a_provider_config_pulls_in_its_whole_folder(repo):
    result = change_analysis.analyse(repo, ["hosts/hetzner/_config.yml"])
    assert set(result["hosts"]) == {"web01", "db01"}
    assert "nas01" not in result["hosts"]


def test_changing_a_role_pulls_in_the_hosts_that_use_it(repo):
    result = change_analysis.analyse(repo, ["ansible/roles/firewall/tasks/main.yml"])
    assert set(result["hosts"]) == {"web01", "db01"}


def test_touching_shared_tooling_expands_to_everything(repo):
    result = change_analysis.analyse(repo, ["scripts/deploy.sh"])
    assert result["everything"] is True
    assert set(result["hosts"]) == {"web01", "db01", "media01", "nas01"}


def test_baremetal_hosts_never_reach_a_provisioning_stack(repo):
    result = change_analysis.analyse(repo, ["hosts/baremetal/nas01.yml"])
    assert result["hosts"] == ["nas01"]
    assert result["provisioned_hosts"] == []
    assert "baremetal" not in result["tofu_stacks"]
