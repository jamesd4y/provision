# openSUSE MicroOS

A host with `os.distribution: microos` is configured at first boot by Ignition
and Combustion rather than converged afterwards by Ansible. MicroOS has no apt,
a read-only `/usr`, and package changes that only take effect after a reboot —
so the usual approach does not apply.

## What gets generated

```bash
python3 scripts/render_ignition.py --host edge01
```

```
build/ignition/edge01/
├── config.ign            Ignition: users, files, systemd units
└── combustion/
    └── script            Combustion: a script run inside transactional-update
```

Both, for every MicroOS host, because which one runs depends on how the machine
boots:

| Mechanism | Read from | Used for |
|---|---|---|
| **Ignition** | instance userdata | Hetzner (platform `hetzner`) and Proxmox (platform `proxmoxve`) — the same delivery path cloud-init already uses |
| **Combustion** | a config drive labelled `combustion` or `ignition` | USB, ISO or an attached disk — the baremetal path |

They converge on the same state. Combustion installs packages directly, because
it already runs inside a `transactional-update shell`. Ignition cannot run
arbitrary commands, so it writes a `provision-bootstrap.service` that installs
the same packages, reboots into the new snapshot, and finishes on the way back
up. If Combustion ran, the bootstrap finds the packages present and skips
straight to enabling them.

## Delivery is automatic

Nothing extra to do. `scripts/render_tfvars.py` embeds the Ignition config for
MicroOS hosts, and each stack sends it as userdata instead of cloud-config:

```hcl
user_data = each.value.ignition != "" ? replace(
  each.value.ignition, "__TS_AUTH_KEY__", var.tailscale_auth_key
) : templatefile(".../cloud-init.yaml.tftpl", { ... })
```

The Tailscale bootstrap key is a placeholder in every generated file and is
substituted at apply time, so no key is written to disk by this repo.

## You need an image first

Neither provider ships MicroOS.

**Proxmox** — build a template from the MicroOS qcow2 once per node:

```bash
# on the Proxmox node
VMID=9100
curl -fsSLO https://download.opensuse.org/tumbleweed/appliances/openSUSE-MicroOS.x86_64-OpenStack-Cloud.qcow2
qm create $VMID --name microos-latest --memory 4096 --cores 2 \
  --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-single --ostype l26
qm importdisk $VMID openSUSE-MicroOS.x86_64-OpenStack-Cloud.qcow2 local-zfs
qm set $VMID --scsi0 local-zfs:vm-$VMID-disk-0 --boot order=scsi0
qm set $VMID --ide2 local-zfs:cloudinit --serial0 socket --vga serial0
qm set $VMID --agent enabled=1
qm template $VMID
```

The name must match `defaults.template` or the host's
`provider_options.template`.

**Hetzner** — build a snapshot (Packer is the usual route) and set
`os.image` to it, or name it `microos-latest` to match the default.

Either way the image must have the `ignition` and `combustion` dracut modules,
which the official MicroOS cloud images do.

## What Ansible still does

The roles run, but every package-installing task is skipped
(`provision_microos`). Ansible manages configuration and the service layer:
Docker's daemon config, the `edge` network, stack directories, the Vector and
vmagent stacks, and Tailscale's ongoing settings.

The one structural difference is the firewall. MicroOS ships **firewalld**, not
nftables, so the `firewall` role does not apply — the Ignition bootstrap
configures firewalld with the same policy instead:

```bash
firewall-cmd --permanent --zone=trusted --change-interface=tailscale0
firewall-cmd --permanent --zone=public --remove-service=ssh
firewall-cmd --permanent --zone=public --add-port=41641/udp
```

Trust the tailnet, no public SSH, allow direct Tailscale connections. A test
checks a MicroOS host is firewalled this way rather than silently having no
firewall at all.

## Updates

MicroOS updates itself: `transactional-update` applies changes to a new
snapshot and `rebootmgr` reboots into it on a schedule. There is no
`unattended-upgrades` equivalent and the base role skips that task. If you want
to control when reboots happen, configure `rebootmgrd`'s maintenance window
through an Ignition file rather than by hand.

## Validating before you boot anything

A bad Ignition config on a host with no public SSH is unrecoverable without the
provider console, so CI checks it with the real validator:

```bash
python3 scripts/render_ignition.py --all
ignition-validate build/ignition/edge01/config.ign
bash -n build/ignition/edge01/combustion/script
```

## Making a config drive by hand

For baremetal or an ISO install:

```bash
truncate -s 8M combustion.img
mkfs.ext4 -L combustion combustion.img       # the label is what Combustion looks for
mkdir -p mnt && sudo mount combustion.img mnt
sudo mkdir -p mnt/combustion mnt/ignition
sudo cp build/ignition/edge01/combustion/script mnt/combustion/script
sudo cp build/ignition/edge01/config.ign mnt/ignition/config.ign
sudo umount mnt
```

Attach it at first boot. Combustion accepts the label `ignition` as a fallback,
so one drive carries both.
