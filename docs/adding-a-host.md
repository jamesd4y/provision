# Adding a host

## 1. Pick the folder

The folder is where the machine comes from.

| Folder | `kind` | What happens on merge |
|---|---|---|
| `hosts/hetzner/` | `hetzner` | OpenTofu creates a Hetzner Cloud VM |
| `hosts/pve01/` | `proxmox` | OpenTofu clones a template on that node |
| `hosts/baremetal/` | `baremetal` | Nothing is created; the machine must already exist |

A new Hetzner project or a second Proxmox node gets its own folder with its own
`_config.yml` — see [provisioning.md](provisioning.md).

## 2. Write the file

The filename is the hostname: `hosts/hetzner/web02.yml` describes `web02`.
Validation rejects any disagreement between the two.

```yaml
hostname: web02
fqdn: web02.example.com
description: Second edge node.
tags: [prod, edge]

hardware:
  cpu: 2                 # minimums, not exact requests
  memory: 4096           # MiB
  storage:
    - { name: root, size: 40, type: ssd, mount: / }
    - { name: data, size: 100, type: network, mount: /srv, format: ext4 }

os:
  distribution: debian
  version: "13"

network:
  firewall:
    - { name: ssh, port: "22", source: [10.10.0.0/16] }
    - { name: https, port: "443" }

ansible:
  roles: [base, docker, firewall, node_exporter]
  vars:
    swap_size_mb: 2048

dns:
  zone: example.com
  record: web02

services:
  - name: traefik
    secrets: { CF_DNS_API_TOKEN: cloudflare/acme_dns_token }
    env: { ACME_EMAIL: ops@example.com }
```

Anything omitted falls back to `defaults:` in the folder's `_config.yml`. Host
values win, except `tags`, which are added to the folder's.

### hardware

`cpu` and `memory` are **minimums**. On Hetzner they select the cheapest
non-deprecated server type that satisfies both; `tofu plan` prints what was
chosen, and `provider_options.server_type` overrides it. On Proxmox they set the
VM directly.

`storage[0]` (or whichever entry mounts `/`) is the root disk. The rest become
Hetzner volumes or extra Proxmox disks. `format: none` means *never touch this
filesystem* — use it for a pre-existing ZFS pool or an encrypted volume.

Formatting and mounting extra disks is the `storage` role's job; add it to
`ansible.roles` and give it a device if autodetection is not safe for you:

```yaml
ansible:
  roles: [base, storage, docker]
  vars:
    provision_disk_devices:
      data: /dev/disk/by-id/scsi-0HC_Volume_12345   # printed by `tofu output volumes`
```

### network

Cloud hosts leave `ipv4` unset — OpenTofu owns that address and writes it into
`build/host_ips.json` after the apply. Validation warns if you set it anyway.

Baremetal hosts **must** carry `network.ipv4` (or a resolvable `fqdn`): nothing
can discover a machine it did not create.

`firewall` rules are applied twice: at the cloud firewall, where supported, and
by the `firewall` role in nftables. Note that neither filters *published
container ports* — Docker DNATs those before nftables sees them. Bind a
container port to a private address (as `services/postgres.yml` does) when it
should not be public.

### services

The host declares what runs on it. Each entry names a `services/<name>.yml`, and
supplies whatever that stack needs from *this* host: domains, plain env values,
and secret keys. See [adding-a-service.md](adding-a-service.md).

## 3. Check it before opening the PR

```bash
python3 scripts/validate.py                    # schema + cross-references
python3 scripts/inventory.py --host web02      # what Ansible will see
python3 scripts/render_stack.py --host web02   # what lands in /opt/stacks
python3 scripts/render_tfvars.py --stack hetzner && \
  jq '.hosts.web02' tofu/hetzner/vars/hetzner.tfvars.json
```

The PR pipeline posts the `tofu plan` as a comment. Read it: a plan that
*replaces* an existing host rather than creating a new one means you changed
something immutable.

## 4. Merge

`provision` creates the machine, `dns` publishes its records, `configure` runs
Ansible, `deploy` ships its stacks. Watch them in Woodpecker;
[ci.md](ci.md) explains what each stage does and what it needs.

## Taking a host out of service

```yaml
enabled: false
```

It leaves the inventory, keeps its DNS and its machine, and stops being
deployed to. Reversible, and the right first step when something is misbehaving.

**Deleting the file destroys the machine and its data.** Both provisioning
stacks set `prevent_destroy`, so OpenTofu refuses and tells you so. To really
remove a host: drain it, take a backup you have restored from at least once,
remove the `prevent_destroy` line for that apply, merge the deletion, then put
the line back.
