"""Logging pipeline invariants.

A log pipeline fails quietly: nobody notices a host stopped shipping until they
go looking for a log that is not there. These are the mistakes worth catching
in CI rather than during an incident.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import render_stack  # noqa: E402
from lib import model  # noqa: E402


def test_every_host_that_runs_containers_also_runs_a_collector(repo):
    missing = [
        host.name
        for host in repo.hosts.values()
        if host.enabled
        and host.service_bindings
        and not any(b["name"] == "vector" for b in host.service_bindings)
    ]
    assert missing == [], f"these hosts run stacks but ship no logs: {missing}"


def test_the_collector_is_deployed_before_the_stacks_it_watches(repo):
    for host in repo.hosts.values():
        if not host.enabled:
            continue
        wanted = [b["name"] for b in host.service_bindings]
        if "vector" not in wanted:
            continue
        order = render_stack.order_services(repo, host, wanted)
        assert order[0] == "vector", f"{host.name} deploys {order[0]} before its collector"


def test_every_collector_has_an_endpoint_to_ship_to(repo):
    for host in repo.hosts.values():
        if not host.enabled:
            continue
        binding = repo.binding(host, "vector")
        if binding is None:
            continue
        plain, _ = model.env_for(repo, host, repo.services["vector"])
        assert plain.get("VICTORIALOGS_ENDPOINT"), f"{host.name} has no VICTORIALOGS_ENDPOINT"
        assert plain["VICTORIALOGS_ENDPOINT"].startswith("http")


def test_collectors_ship_to_a_store_this_repo_actually_runs(repo):
    """Catch an endpoint pointing at a host that no longer runs VictoriaLogs."""
    # Endpoints are MagicDNS names now, so map every address a store answers on
    # back to the host serving it.
    stores: dict[str, str] = {}
    for host in repo.hosts_running("victoria-logs"):
        plain, _ = model.env_for(repo, host, repo.services["victoria-logs"])
        port = plain["VICTORIALOGS_PORT"]
        if host.on_tailnet:
            stores[f"{host.ansible_host}:{port}"] = host.name
        bind = plain["VICTORIALOGS_BIND"]
        if bind not in ("0.0.0.0", "::", "127.0.0.1"):
            stores[f"{bind}:{port}"] = host.name
        for attr in ("private_ipv4", "ipv4"):
            if bind == "0.0.0.0" and host.network.get(attr):
                stores[f"{host.network[attr]}:{port}"] = host.name

    for host in repo.hosts.values():
        if not host.enabled or repo.binding(host, "vector") is None:
            continue
        plain, _ = model.env_for(repo, host, repo.services["vector"])
        target = plain["VICTORIALOGS_ENDPOINT"].removeprefix("http://").removeprefix("https://")
        assert target in stores, (
            f"{host.name} ships to {target}, which no host in this repo serves. "
            f"Known stores: {sorted(stores)}"
        )


def test_one_store_serves_the_whole_fleet(repo):
    """Two stores means two places to look; the tailnet removed the reason for it."""
    endpoints = set()
    for host in repo.hosts.values():
        if not host.enabled or repo.binding(host, "vector") is None:
            continue
        plain, _ = model.env_for(repo, host, repo.services["vector"])
        endpoints.add(plain["VICTORIALOGS_ENDPOINT"])
    assert len(endpoints) == 1, f"collectors are split across {sorted(endpoints)}"


def test_the_log_store_is_never_published_by_accident(repo):
    """VictoriaLogs has no authentication, so a domain binding must not slip in."""
    service = repo.services["victoria-logs"]
    assert service.ingress == "none"
    for host in repo.hosts_running("victoria-logs"):
        binding = repo.binding(host, "victoria-logs")
        assert not binding.get("domains"), f"{host.name} publishes the log store"


def test_the_log_store_is_never_bound_where_the_public_internet_can_reach_it(repo):
    """0.0.0.0 is allowed only where there is no public v4 to bind to.

    db01 has ipv4_enabled: false, so every interface means the Hetzner private
    network and the tailnet. Enabling public IPv4 on such a host would silently
    publish an unauthenticated log store, so pair the two here.
    """
    for host in repo.hosts_running("victoria-logs"):
        plain, _ = model.env_for(repo, host, repo.services["victoria-logs"])
        bind = plain["VICTORIALOGS_BIND"]
        assert bind != "", f"{host.name} has an empty bind address"
        if bind in ("0.0.0.0", "::"):
            assert host.network.get("ipv4_enabled") is False, (
                f"{host.name} binds the log store to {bind} while having a public "
                f"IPv4 — that publishes an unauthenticated log store"
            )


def test_a_host_serving_the_log_store_keeps_it_off_the_public_internet(repo):
    """Reachable over the tailnet, or via a rule scoped to a private network —
    never by a rule open to the world."""
    for host in repo.hosts_running("victoria-logs"):
        plain, _ = model.env_for(repo, host, repo.services["victoria-logs"])
        port = plain["VICTORIALOGS_PORT"]
        rules = [r for r in host.network.get("firewall", []) if r["port"] == port]
        assert host.on_tailnet or rules, (
            f"{host.name} serves {port} but is not on the tailnet and has no firewall rule"
        )
        for rule in rules:
            for cidr in rule.get("source", ["0.0.0.0/0"]):
                assert not cidr.startswith(("0.0.0.0", "::/0")), (
                    f"{host.name} opens the log store to the internet"
                )


def test_stream_fields_stay_low_cardinality(repo):
    """One VictoriaLogs stream per container restart would wreck ingestion."""
    config = yaml.safe_load(repo.services["vector"].path.read_text())
    content = config["configs"]["vector_config"]["content"]
    line = next(ln for ln in content.splitlines() if "_stream_fields" in ln)
    fields = {f.strip() for f in line.split(":", 1)[1].split(",")}
    assert fields == {"host", "stack", "service"}
    forbidden = {"container_id", "container_created_at", "message", "timestamp", "request_id"}
    assert not fields & forbidden


def test_the_collector_never_reads_its_own_output(repo):
    """Vector shipping its own error logs is an unbounded feedback loop."""
    config = yaml.safe_load(repo.services["vector"].path.read_text())
    content = config["configs"]["vector_config"]["content"]
    assert "exclude_containers: [${VECTOR_EXCLUDE_CONTAINERS}]" in content
    plain, _ = model.env_for(repo, repo.hosts["web01"], repo.services["vector"])
    assert "vector" in plain["VECTOR_EXCLUDE_CONTAINERS"].split(",")


def test_vectors_disk_buffer_meets_the_minimum_vector_enforces(repo):
    plain, _ = model.env_for(repo, repo.hosts["web01"], repo.services["vector"])
    assert int(plain["VECTOR_BUFFER_BYTES"]) >= 268435488


def test_the_docker_socket_is_mounted_read_only(repo):
    compose = repo.services["vector"].compose
    mounts = compose["services"]["vector"]["volumes"]
    socket = next(m for m in mounts if "docker.sock" in m)
    assert socket.endswith(":ro"), "the collector must not be able to control Docker"


def test_syslog_ingestion_is_off_unless_a_host_asks_for_it(repo):
    """The listener exists for the log-driver path; it must not be on by default."""
    service = repo.services["victoria-logs"]
    assert service.optional_env["VICTORIALOGS_SYSLOG_TCP"] == ""
    assert service.optional_env["VICTORIALOGS_SYSLOG_BIND"] == "127.0.0.1"

    for host in repo.hosts_running("victoria-logs"):
        plain, _ = model.env_for(repo, host, service)
        if plain["VICTORIALOGS_SYSLOG_TCP"]:
            assert plain["VICTORIALOGS_SYSLOG_BIND"] not in ("0.0.0.0", "::", "")
            ports = [r["port"] for r in host.network.get("firewall", [])]
            assert plain["VICTORIALOGS_SYSLOG_PORT"] in ports, (
                f"{host.name} accepts syslog but has no firewall rule for it"
            )


def test_a_host_never_both_ships_and_forwards_its_logs(repo):
    """Running Vector *and* a remote log-driver stores every line twice."""
    for host in repo.hosts.values():
        if not host.enabled:
            continue
        driver = (host.ansible.get("vars") or {}).get("docker_log_driver", "json-file")
        if driver not in ("json-file", "local"):
            assert repo.binding(host, "vector") is None, (
                f"{host.name} uses the {driver} log driver and also runs a collector"
            )
