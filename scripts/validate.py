#!/usr/bin/env python3
"""Validate hosts/, services/ and cloudflare/ before anything is allowed to run.

Two passes:
  1. JSON Schema, so every file is shaped the way schemas/ says it is.
  2. Cross-references, which schemas cannot express: a host binding a service
     that does not exist, a domain in a zone we do not manage, a required env
     var nobody supplies, two hosts claiming the same singleton stack.

Exit code 0 means the pipeline may proceed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from lib import model
from lib.model import ModelError, Repo


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def warn(self, where: str, message: str) -> None:
        self.warnings.append(f"{where}: {message}")

    def ok(self) -> bool:
        return not self.errors


def build_registry() -> Registry:
    registry = Registry()
    for path in model.SCHEMA_DIR.glob("*.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(schema)
        # Register under both the $id and the bare filename so "host.schema.json#/..."
        # resolves from a sibling schema.
        registry = registry.with_resource(schema["$id"], resource)
        registry = registry.with_resource(path.name, resource)
    return registry


def validator_for(name: str, registry: Registry) -> Draft202012Validator:
    schema = json.loads((model.SCHEMA_DIR / name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=registry)


def schema_pass(report: Report, registry: Registry) -> None:
    provider_v = validator_for("provider.schema.json", registry)
    host_v = validator_for("host.schema.json", registry)
    service_v = validator_for("service.schema.json", registry)
    zones_v = validator_for("zones.schema.json", registry)

    for entry in sorted(model.HOSTS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        config = entry / model.PROVIDER_CONFIG_NAME
        if not config.is_file():
            report.error(f"hosts/{entry.name}", "missing _config.yml")
            continue
        _check(report, provider_v, config, model.read_yaml(config))
        for path in sorted(entry.glob("*.yml")):
            if path.name == model.PROVIDER_CONFIG_NAME:
                continue
            _check(report, host_v, path, model.read_yaml(path))

    for path in sorted(model.SERVICES_DIR.glob("*.yml")):
        _check(report, service_v, path, model.read_yaml(path))

    zones_path = model.CLOUDFLARE_DIR / "zones.yml"
    if zones_path.is_file():
        _check(report, zones_v, zones_path, model.read_yaml(zones_path))


def _check(report: Report, validator: Draft202012Validator, path: Path, data: dict) -> None:
    rel = str(path.relative_to(model.REPO_ROOT))
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        location = ".".join(str(p) for p in error.path) or "<root>"
        report.error(rel, f"{location}: {error.message}")


def cross_reference_pass(report: Report, repo: Repo) -> None:
    zone_names = set((repo.zones.get("zones") or {}).keys())

    for host in repo.hosts.values():
        rel = str(host.path.relative_to(model.REPO_ROOT))
        provider = host.provider

        if provider.kind == "baremetal" and not host.network.get("ipv4") and not host.raw.get("fqdn"):
            report.error(rel, "baremetal hosts need network.ipv4 or fqdn — nothing can create them")

        if provider.provisioned and host.network.get("ipv4"):
            report.warn(
                rel,
                "network.ipv4 is set on a provisioned host; OpenTofu owns that address "
                "and the value here is only documentation",
            )

        if not host.hardware.get("storage"):
            report.warn(rel, "no hardware.storage declared; the provider default disk will be used")

        seen: set[str] = set()
        for binding in host.service_bindings:
            name = binding["name"]
            if name in seen:
                report.error(rel, f"service '{name}' is bound twice")
            seen.add(name)

            service = repo.services.get(name)
            if service is None:
                report.error(rel, f"service '{name}' has no services/{name}.yml")
                continue

            for dependency in service.requires:
                if dependency not in seen and dependency not in {b["name"] for b in host.service_bindings}:
                    report.error(
                        rel,
                        f"service '{name}' requires '{dependency}', which this host does not run",
                    )

            supplied = set((binding.get("env") or {}).keys())
            supplied |= set((binding.get("secrets") or {}).keys())
            supplied |= set(service.optional_env.keys())
            for required in service.required_env:
                if required not in supplied:
                    report.error(
                        rel,
                        f"service '{name}' requires env '{required}' — add it under env: or secrets:",
                    )

            for ref in (binding.get("secrets") or {}).values():
                if not model.SECRET_REF.match(str(ref)):
                    report.error(rel, f"service '{name}': '{ref}' is not a valid secret key")

            for domain in binding.get("domains") or []:
                if service.ingress == "none":
                    report.error(
                        rel,
                        f"service '{name}' declares ingress: none but this host binds a domain to it",
                    )
                zone = domain.get("zone")
                if not zone:
                    report.error(rel, f"domain '{domain['name']}' has no zone and none could be inferred")
                elif zone_names and zone not in zone_names:
                    report.error(
                        rel,
                        f"domain '{domain['name']}' is in zone '{zone}', which is not in cloudflare/zones.yml",
                    )
                if not domain.get("port") and not service.port and service.ingress == "traefik":
                    report.error(
                        rel,
                        f"service '{name}' has no x-provision.port and domain "
                        f"'{domain['name']}' sets none — Traefik cannot route it",
                    )

        host_zone = host.dns.get("zone")
        if host_zone and zone_names and host_zone not in zone_names:
            report.error(rel, f"dns.zone '{host_zone}' is not in cloudflare/zones.yml")

    for service in repo.services.values():
        rel = str(service.path.relative_to(model.REPO_ROOT))
        running = repo.hosts_running(service.name)
        if not running:
            report.warn(rel, "no host binds this service; it will never be deployed")
        if service.meta.get("singleton") and len(running) > 1:
            report.error(
                rel,
                "declared singleton but bound on " + ", ".join(h.name for h in running),
            )
        if service.router_service not in service.compose_services:
            report.error(
                rel,
                f"x-provision.router_service '{service.router_service}' is not a compose service",
            )

    all_domains: dict[str, str] = {}
    for host in repo.hosts.values():
        for binding in host.service_bindings:
            for domain in binding.get("domains") or []:
                key = domain["name"]
                owner = f"{host.name}/{binding['name']}"
                if key in all_domains:
                    report.error(
                        "cloudflare",
                        f"domain '{key}' is claimed by both {all_domains[key]} and {owner}",
                    )
                all_domains[key] = owner


def main() -> int:
    report = Report()
    try:
        registry = build_registry()
        schema_pass(report, registry)
        repo = model.load_repo()
        cross_reference_pass(report, repo)
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for warning in report.warnings:
        print(f"warning: {warning}")
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)

    if report.ok():
        print(
            f"ok: {len(repo.providers)} providers, {len(repo.hosts)} hosts, "
            f"{len(repo.services)} services, {len(report.warnings)} warnings"
        )
        return 0
    print(f"\n{len(report.errors)} error(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
