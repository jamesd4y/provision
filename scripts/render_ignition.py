#!/usr/bin/env python3
"""Generate Ignition and Combustion configs for openSUSE MicroOS hosts.

MicroOS has no apt and no mutable /usr, so it is configured at first boot rather
than converged afterwards. Two mechanisms, and this writes both:

  config.ign         Ignition: declarative — users, files, systemd units. This
                     is what Hetzner and Proxmox deliver, because Ignition reads
                     it from instance userdata on both (platform ids `hetzner`
                     and `proxmoxve`).
  combustion/script  Combustion: a shell script run inside a
                     `transactional-update shell`, which is how packages get
                     installed. Delivered on a config drive labelled
                     `combustion` or `ignition` — so USB, ISO or an attached
                     disk, which is the baremetal path.

Both are generated for every MicroOS host. Which one actually runs depends on
how the machine is booted; the Ignition config carries a bootstrap unit that
installs the same packages if Combustion never ran, so either path converges.

  scripts/render_ignition.py --host microos01
  scripts/render_ignition.py --all --out build/ignition
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import model
from lib.model import Host, ModelError, Repo

IGNITION_VERSION = "3.4.0"

# The provisioning stacks embed this config and swap the placeholder for a
# freshly minted key at apply time, so no key is ever written to disk here.
AUTH_KEY_PLACEHOLDER = "__TS_AUTH_KEY__"

# Packages MicroOS does not ship but this repo needs on every host.
BASE_PACKAGES = ["docker", "docker-compose", "tailscale", "python3", "sudo", "curl", "rsync"]

BOOTSTRAP_MARKER = "/var/lib/provision/bootstrap-done"


def data_url(content: str) -> str:
    """Inline file contents as an RFC 2397 data URL, which is what Ignition takes."""
    return "data:," + urllib.parse.quote(content, safe="")


def file_entry(path: str, content: str, mode: int = 0o644, overwrite: bool = True) -> dict:
    return {
        "path": path,
        "overwrite": overwrite,
        # Ignition takes mode as a decimal integer, so 0644 is 420. Writing it
        # as an octal literal here and converting keeps the source readable.
        "mode": mode,
        "contents": {"source": data_url(content)},
    }


def sshd_config(ssh_user: str) -> str:
    return (
        "# Managed by github.com/jamesd4y/provision — local edits are overwritten.\n"
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
        "PermitRootLogin no\n"
        "PubkeyAuthentication yes\n"
        "X11Forwarding no\n"
        "MaxAuthTries 4\n"
        "ClientAliveInterval 300\n"
        "ClientAliveCountMax 2\n"
        f"AllowUsers {ssh_user}\n"
    )


def identity(host: Host) -> str:
    return (
        json.dumps(
            {
                "hostname": host.name,
                "fqdn": host.fqdn,
                "provider": host.provider.name,
                "kind": host.provider.kind,
                "os": "microos",
                "managed_by": "github.com/jamesd4y/provision",
            },
            indent=2,
        )
        + "\n"
    )


def bootstrap_script(host: Host, tailnet: dict, auth_key: str) -> str:
    """Installs packages and joins the tailnet, in whichever order is possible.

    On MicroOS a package install lands in a new snapshot and only takes effect
    after a reboot, so this runs twice: once to install and reboot, once to
    enable and join. The marker file makes the second run the last one.
    """
    tags = ",".join(host.tailscale.get("tags") or [(tailnet.get("tags") or {}).get("server", "tag:server")])
    ts_hostname = host.tailscale_hostname
    packages = " ".join(BASE_PACKAGES)

    join = ""
    if host.on_tailnet:
        join = f"""
# Join the tailnet. Without this the host has no way in: MicroOS hosts carry no
# public SSH rule either.
if ! tailscale status >/dev/null 2>&1; then
  if [ -n "${{TS_AUTH_KEY:-}}" ]; then
    tailscale up --auth-key="${{TS_AUTH_KEY}}?ephemeral=false&preauthorized=true" \\
      --hostname={ts_hostname} --advertise-tags={tags} --accept-dns=true
  else
    echo "provision: no tailscale auth key; this host will not be reachable" >&2
  fi
fi
"""

    return f"""#!/bin/bash
# Managed by github.com/jamesd4y/provision. Runs at boot until it completes.
set -euo pipefail

TS_AUTH_KEY='{auth_key}'

if [ -e {BOOTSTRAP_MARKER} ]; then
  exit 0
fi

# transactional-update builds a new snapshot; the packages are not usable until
# the machine boots into it. So: install, reboot, and let this unit run again.
missing=""
for pkg in {packages}; do
  rpm -q "$pkg" >/dev/null 2>&1 || missing="$missing $pkg"
done

if [ -n "$missing" ]; then
  echo "provision: installing$missing"
  transactional-update --non-interactive pkg install $missing
  echo "provision: rebooting into the new snapshot"
  systemctl reboot
  exit 0
fi

systemctl enable --now docker.service
systemctl enable --now tailscaled.service
{join}
# MicroOS ships firewalld rather than nftables, so the policy the firewall role
# applies elsewhere is expressed here instead: the tailnet is trusted, and
# nothing public may reach SSH.
if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --zone=trusted --change-interface=tailscale0 || true
  firewall-cmd --permanent --zone=public --remove-service=ssh || true
  firewall-cmd --permanent --zone=public --add-port=41641/udp || true
  firewall-cmd --reload || true
fi

install -d -m 0750 /opt/stacks
docker network inspect edge >/dev/null 2>&1 || docker network create edge

install -d -m 0755 "$(dirname {BOOTSTRAP_MARKER})"
date -Is > {BOOTSTRAP_MARKER}
echo "provision: bootstrap complete"
"""


def combustion_script(host: Host, tailnet: dict, auth_key: str) -> str:
    """The config-drive path. Combustion already runs inside a
    transactional-update shell, so packages install without a reboot dance."""
    tags = ",".join(host.tailscale.get("tags") or [(tailnet.get("tags") or {}).get("server", "tag:server")])
    packages = " ".join(BASE_PACKAGES)
    join = ""
    if host.on_tailnet and auth_key:
        join = f"""
tailscale up --auth-key='{auth_key}?ephemeral=false&preauthorized=true' \\
  --hostname={host.tailscale_hostname} --advertise-tags={tags} --accept-dns=true || \\
  echo "provision: tailscale join failed, fix it from the console" >&2
"""

    return f"""#!/bin/bash
# combustion: network
# Managed by github.com/jamesd4y/provision.
#
# Runs once, at first boot, inside a transactional-update shell. Anything that
# needs a package belongs here rather than in Ignition.
set -euo pipefail

# Show progress on the console — a silent first boot is impossible to debug.
exec > >(exec tee -a /dev/tty0) 2>&1

echo "provision: configuring {host.name}"

zypper --non-interactive install --no-recommends {packages}

systemctl enable sshd.service
systemctl enable docker.service
systemctl enable tailscaled.service
{join}
echo "Configured by provision at $(date -Is)" > /etc/issue.d/provision

# Close outputs and let tee finish, or the boot can hang here.
exec 1>&- 2>&-; wait
"""


def render_ignition(repo: Repo, host: Host, auth_key: str) -> dict:
    ssh_user = host.ansible.get("user", "deploy")
    ssh_keys = [key["public_key"] for key in host.provider.ssh_keys]
    timezone = host.os.get("timezone", "UTC")

    config = {
        "ignition": {"version": IGNITION_VERSION},
        "passwd": {
            "users": [
                {
                    "name": ssh_user,
                    # wheel for sudo, docker so the deploy user can talk to the
                    # daemon without a second hop through root.
                    "groups": ["wheel", "docker"],
                    "sshAuthorizedKeys": ssh_keys,
                }
            ]
        },
        "storage": {
            "files": [
                file_entry("/etc/hostname", f"{host.name}\n"),
                file_entry(
                    f"/etc/sudoers.d/90-{ssh_user}",
                    f"{ssh_user} ALL=(ALL) NOPASSWD:ALL\n",
                    mode=0o440,
                ),
                file_entry("/etc/ssh/sshd_config.d/10-provision.conf", sshd_config(ssh_user)),
                file_entry("/etc/provision-host.json", identity(host)),
                file_entry(
                    "/usr/local/bin/provision-bootstrap",
                    bootstrap_script(host, repo.tailnet, auth_key),
                    mode=0o700,
                ),
            ],
            "links": [
                {
                    "path": "/etc/localtime",
                    "overwrite": True,
                    "target": f"/usr/share/zoneinfo/{timezone}",
                }
            ],
        },
        "systemd": {
            "units": [
                {
                    "name": "provision-bootstrap.service",
                    "enabled": True,
                    "contents": f"""[Unit]
Description=Bring a MicroOS host to its declared state
After=network-online.target
Wants=network-online.target
ConditionPathExists=!{BOOTSTRAP_MARKER}

[Service]
Type=oneshot
ExecStart=/usr/local/bin/provision-bootstrap
RemainAfterExit=yes
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
""",
                },
                {"name": "sshd.service", "enabled": True},
            ]
        },
    }

    if host.raw.get("fqdn"):
        config["storage"]["files"].append(
            file_entry("/etc/hosts", f"127.0.0.1 localhost\n127.0.1.1 {host.fqdn} {host.name}\n")
        )

    return config


def render_host(repo: Repo, host: Host, out_root: Path, auth_key: str) -> dict:
    target = out_root / host.name
    (target / "combustion").mkdir(parents=True, exist_ok=True)

    config = render_ignition(repo, host, auth_key)
    ign_path = target / "config.ign"
    ign_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    script_path = target / "combustion" / "script"
    script_path.write_text(combustion_script(host, repo.tailnet, auth_key), encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return {
        "host": host.name,
        "ignition": str(ign_path),
        "combustion": str(script_path),
        "files": len(config["storage"]["files"]),
        "units": len(config["systemd"]["units"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="render one host")
    parser.add_argument("--all", action="store_true", help="render every MicroOS host")
    parser.add_argument("--out", default="build/ignition")
    parser.add_argument(
        "--auth-key",
        default="",
        help="Tailscale bootstrap key. Normally passed by the pipeline; omit to "
        "generate a config that installs packages but does not join the tailnet.",
    )
    args = parser.parse_args()
    if not args.host and not args.all:
        parser.error("pass --host <name> or --all")

    repo = model.load_repo()
    out_root = Path(args.out)
    if not out_root.is_absolute():
        out_root = model.REPO_ROOT / out_root

    if args.host:
        host = repo.hosts.get(args.host)
        if host is None:
            raise ModelError(f"unknown host '{args.host}'")
        hosts = [host]
    else:
        hosts = [h for h in repo.hosts.values() if h.enabled and h.is_microos]

    if not hosts:
        print("no MicroOS hosts to render")
        return 0

    for host in hosts:
        if not host.is_microos:
            raise ModelError(
                f"{host.name} is {host.os.get('distribution')}, not microos — "
                "Ignition only applies to MicroOS hosts"
            )
        result = render_host(repo, host, out_root, args.auth_key)
        rel = Path(result["ignition"]).relative_to(model.REPO_ROOT)
        print(f"{host.name}: {result['files']} files, {result['units']} units -> {rel.parent}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
