# provision

Infrastructure as code for servers and the services that run on them.

Every machine and every container stack in this repo is described once, in YAML,
and Woodpecker CI turns that description into reality. There is no step that
only exists in someone's shell history.

```
hosts/<provider>/_config.yml     how to talk to a provider, and its defaults
hosts/<provider>/<hostname>.yml  one machine: cpu, memory, storage, os, services
services/<name>.yml              one Docker Compose stack, portable across hosts
cloudflare/zones.yml             the DNS zones this repo is allowed to touch
```

## How a change reaches production

```
  hosts/ + services/ + cloudflare/          you edit YAML, open a PR
            │
            ▼
   validate ─────────► plan ────────────────► (review, merge)
   schema, cross-refs, tests   what would change, on the PR
            │
            ▼  on the default branch
   provision ──► dns ──► configure ──► deploy
   OpenTofu      Cloudflare  Ansible     compose over SSH
   makes the     records     users,      renders each stack,
   machines      follow      Docker,     resolves secrets,
   exist         the hosts   firewall    ships it, health-checks
```

Each stage only touches what the diff can reach: editing one service redeploys
that one stack on the hosts that run it, not the fleet. See
[docs/ci.md](docs/ci.md).

## The four layers

| Layer | Tool | Where |
|---|---|---|
| Machines | OpenTofu (Hetzner Cloud, Proxmox) | `tofu/` |
| Host setup | Ansible over SSH | `ansible/` |
| Containers | Docker Compose pushed over SSH | `services/`, `scripts/deploy.sh` |
| DNS | OpenTofu (Cloudflare) | `tofu/cloudflare/` |
| Secrets | Bitwarden Secrets Manager | `scripts/secrets.sh` |

Why these and not the alternatives: [docs/alternatives.md](docs/alternatives.md).

## A host

`hosts/hetzner/web01.yml` — the folder says where the machine comes from, the
file says what it is:

```yaml
hostname: web01
fqdn: web01.example.com

hardware:
  cpu: 2            # picks the cheapest server type that fits
  memory: 4096      # MiB
  storage:
    - { name: root, size: 40, mount: / }
    - { name: data, size: 100, type: network, mount: /srv }

os:
  distribution: debian
  version: "13"

ansible:
  roles: [base, docker, firewall, node_exporter]

services:            # the host declares what runs on it
  - name: traefik
    secrets: { CF_DNS_API_TOKEN: cloudflare/acme_dns_token }
  - name: gitea
    domains: [git.example.com]
    secrets: { GITEA_DB_PASSWORD: gitea/db_password }
```

Three provider folders ship as examples, one of each kind:

- `hosts/hetzner/` — cloud VMs, created and destroyed by OpenTofu
- `hosts/pve01/` — VMs on a Proxmox node, one folder per node
- `hosts/baremetal/` — machines that already exist; described, never created

Adding one: [docs/adding-a-host.md](docs/adding-a-host.md).

## A service

`services/gitea.yml` is an ordinary Compose file with one extra block that
Compose ignores and this repo reads:

```yaml
x-provision:
  description: Gitea, backed by the shared Postgres cluster.
  port: 3000                  # what Traefik routes to
  singleton: true             # refuse to run it in two places
  requires: [traefik]
  required_env: [GITEA_DB_PASSWORD]

services:
  gitea:
    image: gitea/gitea:1.23
    ...
```

No hostname, no domain, no secret appears in it — those come from whichever host
binds the stack, so the same file can run on a Hetzner VM and a Proxmox VM
without being edited. Adding one:
[docs/adding-a-service.md](docs/adding-a-service.md).

## Secrets

The repo stores secret **keys**, never values:

```yaml
secrets:
  GITEA_DB_PASSWORD: gitea/db_password
```

`scripts/secrets.sh` resolves those against Bitwarden Secrets Manager during the
pipeline, writes them mode 0600, and shreds them when the step ends. CI holds
exactly one credential — a Bitwarden machine-account token — and everything else
hangs off it. See [docs/secrets.md](docs/secrets.md).

## Working on it locally

```bash
pip install -r requirements.txt
ansible-galaxy collection install -r ansible/requirements.yml

python3 scripts/validate.py                      # schema + cross-references
python3 -m pytest tests/ -q                      # the rules that schemas cannot express
python3 scripts/inventory.py --list | jq         # the Ansible inventory, from hosts/
python3 scripts/render_stack.py --host web01     # what would land on the host
python3 scripts/changed.py --base origin/main    # what a diff would actually touch
```

Everything under `scripts/` runs the same way in CI as on a laptop, so a
surprise in the pipeline can be reproduced locally. With credentials in your
environment you can also drive a single host by hand:

```bash
export BWS_ACCESS_TOKEN=...            # Bitwarden machine account
scripts/tofu.sh hetzner hetzner plan   # one stack, one provider folder
scripts/deploy.sh web01 gitea          # one stack, one host
cd ansible && ansible-playbook site.yml --limit web01 --check --diff
```

## Reference

- [docs/alternatives.md](docs/alternatives.md) — the options that were on the table, and why these won
- [docs/ci.md](docs/ci.md) — the pipelines, their triggers, and the secrets they need
- [docs/adding-a-host.md](docs/adding-a-host.md)
- [docs/adding-a-service.md](docs/adding-a-service.md)
- [docs/provisioning.md](docs/provisioning.md) — Hetzner, Proxmox templates, adopting a baremetal box
- [docs/secrets.md](docs/secrets.md)
- [docs/cloudflare.md](docs/cloudflare.md)
