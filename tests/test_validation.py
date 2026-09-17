"""Cross-reference rules: things the schema cannot express but that break deploys."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import validate as validator  # noqa: E402
from lib import model  # noqa: E402


def run(fleet=None) -> validator.Report:
    if fleet is not None:
        fleet.use()
    report = validator.Report()
    registry = validator.build_registry()
    validator.schema_pass(report, registry)
    validator.cross_reference_pass(report, model.load_repo())
    return report


def edit(path: Path, mutate) -> None:
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_the_example_fleet_is_valid():
    report = run()
    assert report.errors == []


def test_unknown_service_is_rejected(fleet):
    edit(fleet / "hosts/hetzner/web01.yml",
         lambda d: d["services"].append({"name": "does-not-exist"}))
    assert any("has no services/does-not-exist.yml" in e for e in run(fleet).errors)


def test_domain_outside_a_managed_zone_is_rejected(fleet):
    edit(fleet / "hosts/hetzner/web01.yml",
         lambda d: d["services"][1].update({"domains": ["git.someoneelse.net"]}))
    assert any("not in cloudflare/zones.yml" in e for e in run(fleet).errors)


def test_two_hosts_cannot_claim_the_same_domain(fleet):
    edit(fleet / "hosts/pve01/media01.yml",
         lambda d: d["services"][1].update({"domains": ["status.example.com"]}))
    assert any("claimed by both" in e for e in run(fleet).errors)


def test_missing_required_env_is_rejected(fleet):
    edit(fleet / "hosts/hetzner/web01.yml", lambda d: d["services"][0].pop("secrets"))
    assert any("requires env 'CF_DNS_API_TOKEN'" in e for e in run(fleet).errors)


def test_singleton_service_on_two_hosts_is_rejected(fleet):
    edit(
        fleet / "hosts/pve01/media01.yml",
        lambda d: d["services"].append(
            {"name": "gitea", "secrets": {"GITEA_DB_PASSWORD": "gitea/db_password"}}
        ),
    )
    assert any("declared singleton" in e for e in run(fleet).errors)


def test_unmet_service_dependency_is_rejected(fleet):
    # uptime-kuma requires traefik; take traefik away from the host that runs both.
    edit(fleet / "hosts/pve01/media01.yml",
         lambda d: d.update({"services": [s for s in d["services"] if s["name"] != "traefik"]}))
    assert any("requires 'traefik'" in e for e in run(fleet).errors)


def test_service_bound_twice_is_rejected(fleet):
    edit(fleet / "hosts/hetzner/web01.yml",
         lambda d: d["services"].append({"name": "uptime-kuma"}))
    assert any("bound twice" in e for e in run(fleet).errors)


def test_routing_a_domain_to_an_internal_service_is_rejected(fleet):
    edit(fleet / "hosts/hetzner/db01.yml",
         lambda d: d["services"][0].update({"domains": ["db.example.com"]}))
    assert any("ingress: none" in e for e in run(fleet).errors)


def test_baremetal_without_an_address_is_rejected(fleet):
    def strip(data):
        data["network"].pop("ipv4")
        data["network"].pop("private_ipv4")
        data.pop("fqdn")

    edit(fleet / "hosts/baremetal/nas01.yml", strip)
    assert any("nothing can create them" in e for e in run(fleet).errors)


def test_a_service_nobody_runs_is_only_a_warning(fleet):
    (fleet / "services/orphan.yml").write_text(
        yaml.safe_dump(
            {
                "x-provision": {"description": "nobody runs this", "ingress": "none"},
                "services": {"orphan": {"image": "alpine:3"}},
            }
        )
    )
    report = run(fleet)
    assert report.errors == []
    assert any("no host binds this service" in w for w in report.warnings)
