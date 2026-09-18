"""Load and normalise the declarative model in hosts/, services/ and cloudflare/.

Everything else in this repo (validation, the Ansible inventory, the OpenTofu
tfvars, the Cloudflare records, the compose deploy) reads the repo through this
module, so there is exactly one interpretation of the YAML.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(os.environ.get("PROVISION_ROOT") or Path(__file__).resolve().parents[2])
HOSTS_DIR = REPO_ROOT / "hosts"
SERVICES_DIR = REPO_ROOT / "services"
CLOUDFLARE_DIR = REPO_ROOT / "cloudflare"
TAILSCALE_DIR = REPO_ROOT / "tailscale"
BACKUP_DIR = REPO_ROOT / "backup"
SCHEMA_DIR = REPO_ROOT / "schemas"


def set_root(root: Path) -> None:
    """Point the loader at a different tree.

    Used by the tests to build a deliberately broken fleet in a temp directory
    and check that validation rejects it. PROVISION_ROOT does the same thing
    from the command line, which is handy when reviewing someone else's branch.
    """
    global REPO_ROOT, HOSTS_DIR, SERVICES_DIR, CLOUDFLARE_DIR, TAILSCALE_DIR, BACKUP_DIR
    REPO_ROOT = Path(root)
    HOSTS_DIR = REPO_ROOT / "hosts"
    SERVICES_DIR = REPO_ROOT / "services"
    CLOUDFLARE_DIR = REPO_ROOT / "cloudflare"
    TAILSCALE_DIR = REPO_ROOT / "tailscale"
    BACKUP_DIR = REPO_ROOT / "backup"
    # Schemas always come from the real checkout: a test fixture describes a
    # fleet, it does not get to redefine what a valid fleet is.

PROVIDER_CONFIG_NAME = "_config.yml"

# Distribution+version -> provider image id, used when os.image is not given.
DEFAULT_IMAGES = {
    "hetzner": {
        "debian:13": "debian-13",
        "debian:12": "debian-12",
        "ubuntu:24.04": "ubuntu-24.04",
        "ubuntu:22.04": "ubuntu-22.04",
        "rocky:9": "rocky-9",
        "alma:9": "alma-9",
        "fedora:40": "fedora-40",
        # MicroOS has no Hetzner-provided image: build a snapshot once and name
        # it this, or set os.image per host. docs/microos.md has the steps.
        "microos:latest": "microos-latest",
    },
    "proxmox": {
        "debian:13": "debian-13-cloudinit",
        "debian:12": "debian-12-cloudinit",
        "ubuntu:24.04": "ubuntu-24.04-cloudinit",
        "microos:latest": "microos-latest",
    },
}

SECRET_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")


class ModelError(Exception):
    """A problem with the repo's YAML that a human has to fix."""


def deep_merge(base: dict, override: dict) -> dict:
    """Merge override onto base. Dicts merge recursively; everything else replaces."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def read_yaml(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ModelError(f"{_rel(path)}: invalid YAML: {exc}") from exc
    if data is None:
        raise ModelError(f"{_rel(path)}: file is empty")
    if not isinstance(data, dict):
        raise ModelError(f"{_rel(path)}: expected a mapping at the top level")
    return data


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


@dataclass
class Provider:
    name: str
    kind: str
    path: Path
    raw: dict
    enabled: bool = True

    @property
    def defaults(self) -> dict:
        return self.raw.get("defaults", {}) or {}

    @property
    def credentials(self) -> dict:
        return self.raw.get("credentials", {}) or {}

    @property
    def ssh_keys(self) -> list[dict]:
        return self.raw.get("ssh_keys", []) or []

    @property
    def provisioned(self) -> bool:
        """baremetal hosts are described here but created by hand."""
        return self.kind != "baremetal"


@dataclass
class Host:
    name: str
    provider: Provider
    path: Path
    raw: dict
    # Set from tailscale/tailnet.yml at load time so a host can work out its own
    # MagicDNS name without every caller having to pass the tailnet around.
    magic_dns_suffix: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.raw.get("enabled", True)) and self.provider.enabled

    @property
    def fqdn(self) -> str:
        return self.raw.get("fqdn") or self.name

    @property
    def hardware(self) -> dict:
        return self.raw.get("hardware", {}) or {}

    @property
    def os(self) -> dict:
        return self.raw.get("os", {}) or {}

    @property
    def network(self) -> dict:
        return self.raw.get("network", {}) or {}

    @property
    def ansible(self) -> dict:
        return self.raw.get("ansible", {}) or {}

    @property
    def dns(self) -> dict:
        return self.raw.get("dns", {}) or {}

    @property
    def tags(self) -> list[str]:
        return self.raw.get("tags", []) or []

    @property
    def service_bindings(self) -> list[dict]:
        return [s for s in (self.raw.get("services") or []) if s.get("enabled", True)]

    @property
    def root_disk(self) -> dict:
        disks = self.hardware.get("storage") or []
        for disk in disks:
            if disk.get("mount", "/") == "/":
                return disk
        return disks[0] if disks else {"name": "root", "size": 20, "type": "ssd"}

    @property
    def extra_disks(self) -> list[dict]:
        return [d for d in (self.hardware.get("storage") or []) if d is not self.root_disk]

    @property
    def is_microos(self) -> bool:
        """MicroOS is configured at first boot by Ignition/Combustion, not by apt."""
        return self.os.get("distribution") == "microos"

    @property
    def tailscale(self) -> dict:
        return self.raw.get("tailscale", {}) or {}

    @property
    def on_tailnet(self) -> bool:
        return bool(self.tailscale.get("enabled", False))

    @property
    def tailscale_hostname(self) -> str:
        return self.tailscale.get("hostname") or self.name

    @property
    def public_ssh(self) -> bool:
        """Whether port 22 is open to the world. Off unless a host asks for it."""
        return bool(self.network.get("public_ssh", False))

    @property
    def ansible_host(self) -> str | None:
        """Where SSH connects.

        A host on the tailnet is reached by its MagicDNS name: that address works
        from anywhere the tailnet reaches, including a CI runner with no fixed
        egress IP and a host behind NAT. Everything else falls back to the
        addresses the host declares.
        """
        if self.on_tailnet and self.magic_dns_suffix:
            return f"{self.tailscale_hostname}.{self.magic_dns_suffix}"
        net = self.network
        return net.get("private_ipv4") or net.get("ipv4") or net.get("ipv6") or self.raw.get("fqdn")

    @property
    def roles(self) -> list[str]:
        declared = list(self.ansible.get("roles") or [])
        implied = ["base"]
        if self.service_bindings and "docker" not in declared:
            implied.append("docker")
        for role in implied:
            if role not in declared:
                declared.insert(0, role)
        return declared


@dataclass
class Service:
    name: str
    path: Path
    compose: dict

    @property
    def meta(self) -> dict:
        return self.compose.get("x-provision", {}) or {}

    @property
    def description(self) -> str:
        return self.meta.get("description", "")

    @property
    def compose_services(self) -> dict:
        return self.compose.get("services", {}) or {}

    @property
    def router_service(self) -> str:
        explicit = self.meta.get("router_service")
        if explicit:
            return explicit
        if self.name in self.compose_services:
            return self.name
        return next(iter(self.compose_services), self.name)

    @property
    def ingress(self) -> str:
        return self.meta.get("ingress", "traefik")

    @property
    def port(self) -> int | None:
        return self.meta.get("port")

    @property
    def requires(self) -> list[str]:
        return self.meta.get("requires", []) or []

    @property
    def required_env(self) -> list[str]:
        return self.meta.get("required_env", []) or []

    @property
    def optional_env(self) -> dict:
        return self.meta.get("optional_env", {}) or {}

    @property
    def data_dirs(self) -> list[str]:
        return self.meta.get("data_dirs", []) or []

    @property
    def metrics(self) -> dict:
        """Where this stack publishes Prometheus metrics, if it does."""
        return self.meta.get("metrics", {}) or {}

    @property
    def wants_scrape_targets(self) -> bool:
        return bool(self.meta.get("wants_scrape_targets", False))

    @property
    def backup(self) -> dict:
        """How this stack is captured. Absent means nobody has decided yet,
        which validation treats differently from deciding not to."""
        return self.meta.get("backup", {}) or {}

    @property
    def backed_up(self) -> bool:
        return bool(self.backup) and self.backup.get("enabled", True)

    @property
    def backup_paths(self) -> list[str]:
        """Paths under the stack's data root.

        An explicit empty list means "none of the data directory" — which is
        what a stack that dumps itself in a pre hook wants. Only an absent key
        falls back to data_dirs, so `paths: []` cannot silently become
        "back up the live database after all".
        """
        if "paths" in self.backup:
            return list(self.backup["paths"])
        return list(self.data_dirs)


@dataclass
class Repo:
    providers: dict[str, Provider] = field(default_factory=dict)
    hosts: dict[str, Host] = field(default_factory=dict)
    services: dict[str, Service] = field(default_factory=dict)
    zones: dict = field(default_factory=dict)
    tailnet: dict = field(default_factory=dict)
    backup: dict = field(default_factory=dict)

    def hosts_for_provider(self, provider: str) -> list[Host]:
        return [h for h in self.hosts.values() if h.provider.name == provider and h.enabled]

    def hosts_running(self, service: str) -> list[Host]:
        return [
            h
            for h in self.hosts.values()
            if h.enabled and any(b["name"] == service for b in h.service_bindings)
        ]

    def binding(self, host: Host, service: str) -> dict | None:
        for b in host.service_bindings:
            if b["name"] == service:
                return b
        return None


def _normalise_os(host_raw: dict, provider_kind: str) -> None:
    os_block = host_raw.setdefault("os", {})
    os_block.setdefault("timezone", "UTC")
    if os_block.get("image"):
        return
    key = f"{os_block.get('distribution')}:{os_block.get('version')}"
    image = DEFAULT_IMAGES.get(provider_kind, {}).get(key)
    if image:
        os_block["image"] = image


def _normalise_domains(host_raw: dict, default_zone: str | None, zone_names: set[str]) -> None:
    """Domains may be written as plain strings; expand them to the object form."""
    for binding in host_raw.get("services") or []:
        expanded = []
        for domain in binding.get("domains") or []:
            if isinstance(domain, str):
                domain = {"name": domain}
            domain.setdefault("proxied", True)
            domain.setdefault("record", "A")
            if not domain.get("zone"):
                zone = _infer_zone(domain["name"], default_zone, zone_names)
                if zone:
                    domain["zone"] = zone
            expanded.append(domain)
        if expanded:
            binding["domains"] = expanded


def _infer_zone(fqdn: str, default_zone: str | None, zone_names: set[str]) -> str | None:
    """Work out which zone a domain belongs to.

    The managed zones decide, by longest matching suffix: git.example.com lands
    in example.com, and a name under example.co.uk is not mistaken for co.uk.
    Only when no managed zone matches do we fall back to the host's own zone,
    and validation then rejects the result — which is the point. Silently
    filing someoneelse.net under the host's zone would create a record in the
    wrong place.
    """
    matches = [z for z in zone_names if fqdn == z or fqdn.endswith("." + z)]
    if matches:
        return max(matches, key=len)
    if default_zone and (fqdn == default_zone or fqdn.endswith("." + default_zone)):
        return default_zone
    parts = fqdn.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else None


def load_providers() -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    if not HOSTS_DIR.is_dir():
        raise ModelError("hosts/ directory is missing")
    for entry in sorted(HOSTS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        config_path = entry / PROVIDER_CONFIG_NAME
        if not config_path.is_file():
            raise ModelError(f"hosts/{entry.name}/ has no {PROVIDER_CONFIG_NAME}")
        raw = read_yaml(config_path)
        declared = raw.get("provider")
        if declared != entry.name:
            raise ModelError(
                f"{_rel(config_path)}: provider is '{declared}' but the folder is '{entry.name}'"
            )
        providers[entry.name] = Provider(
            name=entry.name,
            kind=raw.get("kind", ""),
            path=entry,
            raw=raw,
            enabled=bool(raw.get("enabled", True)),
        )
    return providers


def load_hosts(
    providers: dict[str, Provider],
    zone_names: set[str] | None = None,
    magic_dns_suffix: str | None = None,
) -> dict[str, Host]:
    hosts: dict[str, Host] = {}
    for provider in providers.values():
        for path in sorted(provider.path.glob("*.yml")):
            if path.name == PROVIDER_CONFIG_NAME:
                continue
            raw = read_yaml(path)
            stem = path.stem
            if raw.get("hostname") != stem:
                raise ModelError(
                    f"{_rel(path)}: hostname is '{raw.get('hostname')}' but the file is '{stem}.yml'"
                )
            merged = deep_merge(provider.defaults, raw)
            # Lists normally replace on merge, but tags are additive: a host keeps
            # the folder's tags (cloud, homelab, ...) and adds its own.
            merged["tags"] = sorted(
                set(provider.defaults.get("tags") or []) | set(raw.get("tags") or [])
            )
            _normalise_os(merged, provider.kind)
            _normalise_domains(
                merged, (merged.get("dns") or {}).get("zone"), zone_names or set()
            )
            if stem in hosts:
                raise ModelError(
                    f"{_rel(path)}: hostname '{stem}' already defined in "
                    f"{_rel(hosts[stem].path)} — hostnames are global"
                )
            hosts[stem] = Host(
                name=stem,
                provider=provider,
                path=path,
                raw=merged,
                magic_dns_suffix=magic_dns_suffix,
            )
    return hosts


def load_services() -> dict[str, Service]:
    services: dict[str, Service] = {}
    if not SERVICES_DIR.is_dir():
        return services
    for path in sorted(SERVICES_DIR.glob("*.yml")):
        compose = read_yaml(path)
        services[path.stem] = Service(name=path.stem, path=path, compose=compose)
    return services


def load_zones() -> dict:
    path = CLOUDFLARE_DIR / "zones.yml"
    if not path.is_file():
        return {}
    return read_yaml(path)


# Where a pre hook writes a dump. Backed up alongside the stack's own paths.
BACKUP_STAGING_ROOT = "/var/lib/provision-backup"


def load_backup() -> dict:
    path = BACKUP_DIR / "config.yml"
    if not path.is_file():
        return {}
    config = read_yaml(path)
    return config if config.get("enabled", True) else {}


def backup_plan(repo: "Repo", host: Host) -> list[dict]:
    """What the backup job on one host actually does, stack by stack.

    Built here rather than in the Ansible role so the same answer is available
    to the inventory, the tests and anyone reading the repo.
    """
    plan = []
    for binding in host.service_bindings:
        service = repo.services.get(binding["name"])
        if service is None or not service.backed_up:
            continue
        data_root = binding.get("volumes_root") or f"/srv/{service.name}"
        backup = service.backup
        staging = f"{BACKUP_STAGING_ROOT}/{service.name}"
        paths = [f"{data_root}/{p}" for p in service.backup_paths]
        # Whatever a pre hook produced has to be in the snapshot, or the hook
        # was pointless.
        if backup.get("pre"):
            paths.append(staging)
        plan.append(
            {
                "stack": service.name,
                "data_root": data_root,
                "staging": staging,
                "paths": paths,
                # Anchored to the stack's data root unless already absolute.
                # An unanchored pattern like "**/tmp" matches any path
                # component anywhere and can silently exclude everything.
                "exclude": [
                    e if e.startswith("/") else f"{data_root}/{e}"
                    for e in backup.get("exclude", [])
                ],
                "pre": backup.get("pre", []),
                "post": backup.get("post", []),
                "stop": bool(backup.get("stop", False)),
            }
        )
    return plan


def load_tailnet() -> dict:
    path = TAILSCALE_DIR / "tailnet.yml"
    if not path.is_file():
        return {}
    config = read_yaml(path)
    return config if config.get("enabled", True) else {}


def load_repo() -> Repo:
    # Zones load first: a domain's zone is inferred from the managed zone list,
    # not from counting dots.
    zones = load_zones()
    zone_names = set((zones.get("zones") or {}).keys())
    tailnet = load_tailnet()
    backup = load_backup()
    providers = load_providers()
    return Repo(
        providers=providers,
        hosts=load_hosts(providers, zone_names, tailnet.get("magic_dns_suffix")),
        services=load_services(),
        zones=zones,
        tailnet=tailnet,
        backup=backup,
    )


# node_exporter is run by an Ansible role rather than a Compose stack, so it has
# no services/*.yml to declare itself. Its port is fixed by that role.
NODE_EXPORTER_PORT = 9100


def scrape_targets(repo: Repo, host: Host) -> list[dict]:
    """Everything worth scraping on one host, in Prometheus file_sd form.

    Every exporter in this repo publishes to loopback, so the address is always
    127.0.0.1 and the only question is which ports exist on this host.
    """
    targets: list[dict] = []

    if "node_exporter" in host.roles:
        targets.append(
            {
                "targets": [f"127.0.0.1:{NODE_EXPORTER_PORT}"],
                "labels": {"job": "node", "__metrics_path__": "/metrics"},
            }
        )

    for binding in host.service_bindings:
        service = repo.services.get(binding["name"])
        if service is None or not service.metrics:
            continue
        metrics = service.metrics
        targets.append(
            {
                "targets": [f"127.0.0.1:{metrics['port']}"],
                "labels": {
                    "job": metrics.get("job") or service.name,
                    "stack": service.name,
                    "__metrics_path__": metrics.get("path", "/metrics"),
                },
            }
        )

    return targets


def env_for(repo: Repo, host: Host, service: Service) -> tuple[dict[str, Any], dict[str, str]]:
    """Return (plain env, secret refs) for one stack on one host.

    Plain env is safe to print and commit to a build artifact; secret refs are
    Bitwarden Secrets Manager keys that scripts/secrets.sh resolves at deploy time.
    """
    binding = repo.binding(host, service.name) or {}
    plain: dict[str, Any] = {
        "STACK_NAME": service.name,
        "HOSTNAME": host.name,
        "DATA_ROOT": binding.get("volumes_root") or f"/srv/{service.name}",
        "TZ": (host.os or {}).get("timezone", "UTC"),
    }
    plain.update(service.optional_env)
    if service.wants_scrape_targets:
        # One line, because a .env value cannot span lines. Compose drops it
        # straight into the file_sd config.
        plain["SCRAPE_TARGETS_JSON"] = json.dumps(scrape_targets(repo, host), separators=(",", ":"))
    plain.update(binding.get("env") or {})
    domains = binding.get("domains") or []
    if domains:
        plain["DOMAIN"] = domains[0]["name"]
        plain["DOMAINS"] = ",".join(d["name"] for d in domains)
    secrets = dict(binding.get("secrets") or {})
    return plain, secrets


def traefik_labels(repo: Repo, host: Host, service: Service) -> list[str]:
    """Router/service labels for a stack, derived from the host's domain bindings."""
    binding = repo.binding(host, service.name) or {}
    domains = binding.get("domains") or []
    if service.ingress != "traefik" or not domains:
        return []
    rule = " || ".join(f"Host(`{d['name']}`)" for d in domains)
    port = next((d["port"] for d in domains if d.get("port")), None) or service.port
    router = service.name
    labels = [
        "traefik.enable=true",
        f"traefik.http.routers.{router}.rule={rule}",
        f"traefik.http.routers.{router}.entrypoints=websecure",
        f"traefik.http.routers.{router}.tls=true",
        f"traefik.http.routers.{router}.tls.certresolver=cloudflare",
    ]
    if port:
        labels.append(f"traefik.http.services.{router}.loadbalancer.server.port={port}")
    return labels
