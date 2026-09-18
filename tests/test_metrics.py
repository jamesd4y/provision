"""Metrics pipeline invariants."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lib import model  # noqa: E402


def test_every_host_running_containers_also_scrapes_them(repo):
    missing = [
        h.name for h in repo.hosts.values()
        if h.enabled and h.service_bindings and repo.binding(h, "vmagent") is None
    ]
    assert missing == [], f"these hosts run stacks but ship no metrics: {missing}"


def test_collectors_agree_on_one_store(repo):
    endpoints = set()
    for host in repo.hosts.values():
        if not host.enabled or repo.binding(host, "vmagent") is None:
            continue
        plain, _ = model.env_for(repo, host, repo.services["vmagent"])
        endpoints.add(plain["VICTORIAMETRICS_ENDPOINT"])
    assert len(endpoints) == 1, f"collectors are split across {sorted(endpoints)}"


def test_scrape_targets_cover_the_host_and_its_containers(repo):
    targets = model.scrape_targets(repo, repo.hosts["web01"])
    jobs = {t["labels"]["job"] for t in targets}
    assert "node" in jobs, "host metrics missing"
    assert "cadvisor" in jobs, "container metrics missing"
    assert "traefik" in jobs, "the proxy on this host exposes metrics but is not scraped"
    for target in targets:
        assert target["targets"][0].startswith("127.0.0.1:"), (
            "exporters are scraped over loopback; anything else is reachable off the host"
        )


def test_a_host_only_scrapes_what_it_actually_runs(repo):
    """nas01 has no Traefik, so it must not be looking for Traefik metrics."""
    jobs = {t["labels"]["job"] for t in model.scrape_targets(repo, repo.hosts["nas01"])}
    assert "traefik" not in jobs
    assert "cadvisor" in jobs


def test_scrape_targets_are_one_line_of_json(repo):
    """A .env value cannot span lines, so the generated file_sd must not either."""
    plain, _ = model.env_for(repo, repo.hosts["web01"], repo.services["vmagent"])
    raw = plain["SCRAPE_TARGETS_JSON"]
    assert "\n" not in raw
    assert isinstance(json.loads(raw), list)


def test_only_vmagent_receives_the_target_list(repo):
    for name, service in repo.services.items():
        if name == "vmagent":
            continue
        plain, _ = model.env_for(repo, repo.hosts["web01"], service)
        assert "SCRAPE_TARGETS_JSON" not in plain, f"{name} does not need the target list"


def test_no_exporter_is_reachable_off_the_host(repo):
    """Every metrics endpoint here is unauthenticated."""
    for name, service in repo.services.items():
        if not service.metrics:
            continue
        compose = service.compose["services"]
        for spec in compose.values():
            for published in spec.get("ports", []) or []:
                if str(service.metrics["port"]) in str(published):
                    assert str(published).startswith("${") or "127.0.0.1" in str(published), (
                        f"{name} publishes its metrics port without a loopback bind: {published}"
                    )


def test_the_metrics_store_is_never_published(repo):
    service = repo.services["victoria-metrics"]
    assert service.ingress == "none"
    for host in repo.hosts_running("victoria-metrics"):
        binding = repo.binding(host, "victoria-metrics")
        assert not binding.get("domains")
        plain, _ = model.env_for(repo, host, service)
        if plain["VICTORIAMETRICS_BIND"] in ("0.0.0.0", "::"):
            assert host.network.get("ipv4_enabled") is False, (
                f"{host.name} would expose an unauthenticated metrics store publicly"
            )


def test_cadvisor_label_cardinality_is_capped(repo):
    """Every compose label becoming a metric label is how a TSDB falls over."""
    cmd = repo.services["cadvisor"].compose["services"]["cadvisor"]["command"]
    assert any("--store_container_labels=false" in c for c in cmd)
    assert any("--whitelisted_container_labels=" in c for c in cmd)
