"""The loader's rules: defaults merge, names match files, hostnames are global."""

from __future__ import annotations

import pytest
import yaml

from lib import model
from lib.model import ModelError


def test_example_fleet_loads(repo):
    assert set(repo.providers) == {"hetzner", "pve01", "baremetal"}
    assert set(repo.hosts) == {"web01", "db01", "media01", "nas01"}
    assert "traefik" in repo.services


def test_provider_defaults_merge_into_hosts(repo):
    # web01 sets neither; both come from hosts/hetzner/_config.yml.
    assert repo.hosts["web01"].os["timezone"] == "Europe/London"
    assert repo.hosts["web01"].ansible["user"] == "deploy"


def test_host_overrides_beat_provider_defaults(repo):
    # db01 declares its own roles rather than inheriting a default set.
    assert "firewall" in repo.hosts["db01"].roles


def test_tags_are_additive_not_replaced(repo):
    # 'cloud' comes from the folder, 'prod'/'edge' from the host.
    assert set(repo.hosts["web01"].tags) == {"cloud", "prod", "edge"}


def test_base_and_docker_are_implied(repo):
    roles = repo.hosts["media01"].roles
    assert roles[0] == "base"
    assert "docker" in roles


def test_os_image_defaults_from_distribution(repo):
    assert repo.hosts["web01"].os["image"] == "debian-13"
    assert repo.hosts["media01"].os["image"] == "debian-13-cloudinit"


def test_bare_domain_strings_expand(repo):
    binding = repo.binding(repo.hosts["web01"], "uptime-kuma")
    assert binding["domains"] == [
        {"name": "status.example.com", "proxied": True, "record": "A", "zone": "example.com"}
    ]


def test_hostname_must_match_filename(fleet):
    path = fleet / "hosts" / "hetzner" / "web01.yml"
    data = yaml.safe_load(path.read_text())
    data["hostname"] = "somethingelse"
    path.write_text(yaml.safe_dump(data))
    fleet.use()
    with pytest.raises(ModelError, match="but the file is"):
        model.load_repo()


def test_duplicate_hostnames_across_providers_are_rejected(fleet):
    source = fleet / "hosts" / "hetzner" / "web01.yml"
    data = yaml.safe_load(source.read_text())
    (fleet / "hosts" / "baremetal" / "web01.yml").write_text(yaml.safe_dump(data))
    fleet.use()
    with pytest.raises(ModelError, match="hostnames are global"):
        model.load_repo()


def test_provider_folder_must_match_its_config(fleet):
    path = fleet / "hosts" / "hetzner" / "_config.yml"
    data = yaml.safe_load(path.read_text())
    data["provider"] = "elsewhere"
    path.write_text(yaml.safe_dump(data))
    fleet.use()
    with pytest.raises(ModelError, match="but the folder is"):
        model.load_repo()


def test_disabled_host_disappears_from_the_fleet(fleet):
    path = fleet / "hosts" / "hetzner" / "db01.yml"
    data = yaml.safe_load(path.read_text())
    data["enabled"] = False
    path.write_text(yaml.safe_dump(data))
    fleet.use()
    repo = model.load_repo()
    assert not repo.hosts["db01"].enabled
    assert "db01" not in [h.name for h in repo.hosts_for_provider("hetzner")]


def test_traefik_labels_use_the_hosts_domains(repo):
    labels = model.traefik_labels(repo, repo.hosts["web01"], repo.services["gitea"])
    assert "traefik.enable=true" in labels
    assert "traefik.http.routers.gitea.rule=Host(`git.example.com`)" in labels
    assert "traefik.http.services.gitea.loadbalancer.server.port=3000" in labels


def test_internal_services_get_no_labels(repo):
    labels = model.traefik_labels(repo, repo.hosts["db01"], repo.services["postgres"])
    assert labels == []


def test_env_separates_plain_values_from_secret_keys(repo):
    plain, secrets = model.env_for(repo, repo.hosts["web01"], repo.services["gitea"])
    assert plain["DOMAIN"] == "git.example.com"
    assert plain["DATA_ROOT"] == "/srv/gitea"
    assert secrets == {
        "GITEA_DB_PASSWORD": "gitea/db_password",
        "GITEA__mailer__PASSWD": "gitea/smtp_password",
    }
    # The secret VALUES are nowhere in the rendered env.
    assert all("password" not in str(v).lower() for v in plain.values())
