# Provisioning

What each provider kind needs before a host file in its folder will work.

## Hetzner Cloud

`hosts/hetzner/_config.yml`:

```yaml
provider: hetzner        # must equal the folder name
kind: hetzner
credentials:
  hcloud_token: hetzner/api_token     # a Bitwarden key
ssh_keys:
  - { name: woodpecker-ci, public_key: "ssh-ed25519 AAAA... ci@provision" }
network:
  private_network: { name: prod, cidr: 10.10.0.0/16, zone: eu-central }
defaults:
  location: nbg1
  os: { distribution: debian, version: "13", timezone: Europe/London }
```

Every key under `ssh_keys` is installed on every host in the folder, by
cloud-init and again by the `base` role, so a host that skipped cloud-init ends
up in the same state.

### How cpu and memory become a server type

`hardware.cpu` and `hardware.memory` are minimums. The stack queries the live
server-type catalogue and picks the smallest non-deprecated type meeting both,
for the right architecture. `tofu output server_types` shows what each host
resolved to, and the plan shows it before you merge.

Two preconditions stop silent mistakes:

- nothing satisfies the request → the error tells you to lower the request or
  set `provider_options.server_type`
- `hardware.storage[root].size` exceeds the type's built-in disk → the error
  tells you to add a volume or pick a bigger type, rather than provisioning a
  host that quietly runs out of space

Extra disks become Hetzner volumes with `delete_protection` on, attached but not
automounted. `tofu output volumes` prints each device path for
`provision_disk_devices`; the `storage` role formats and mounts them.

### Private networking

Hosts with `network.private_ipv4` are attached to the folder's private network
at that address. `db01` shows the useful pattern: `ipv4_enabled: false` plus a
private address means no public IPv4 at all, and it is reachable only from
inside the network.

## Proxmox

One folder per node: `hosts/pve01/`, `hosts/pve02/`. Each gets its own state
address, so a node under maintenance never blocks changes elsewhere.

```yaml
provider: pve01
kind: proxmox
credentials:
  api_token: proxmox/pve01_api_token        # user@realm!tokenid=uuid
  ssh_password: proxmox/pve01_root_password
endpoint:
  url: https://pve01.lan:8006/
  node: pve01
network:
  bridge: vmbr0
  vlan_id: 20
  gateway: 10.20.0.1
  prefix: 24
  nameservers: [10.20.0.1, 1.1.1.1]
  snippet_datastore: local    # needs the "snippets" content type enabled
defaults:
  datastore: local-zfs
  template: debian-13-cloudinit
```

### You need a cloud-init template first

Every host clones one. Build it once per node:

```bash
# on the Proxmox node
VMID=9000
curl -fsSLO https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2

qm create $VMID --name debian-13-cloudinit --memory 2048 --cores 2 \
  --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-single --ostype l26
qm importdisk $VMID debian-13-genericcloud-amd64.qcow2 local-zfs
qm set $VMID --scsi0 local-zfs:vm-$VMID-disk-0 --boot order=scsi0
qm set $VMID --ide2 local-zfs:cloudinit --serial0 socket --vga serial0
qm set $VMID --agent enabled=1
qm template $VMID
```

The name must match `defaults.template` (or a host's
`provider_options.template`). The stack looks up templates by name on the node
and fails with a precondition naming the missing template if it is not there.

### API token permissions

The token needs `VM.Allocate`, `VM.Clone`, `VM.Config.*`, `VM.PowerMgmt`,
`Datastore.AllocateSpace` and `Datastore.Audit`. The SSH password is separate:
the provider shells into the node to upload cloud-init snippets, which the API
alone cannot do.

### Addressing

A host with `network.private_ipv4` gets that address statically, using the
folder's `gateway` and `prefix`. Without one it gets DHCP. A static address and
no gateway is a precondition failure, not a VM you cannot reach.

`provider_options.vm_id` pins the VM id; leave it out and Proxmox allocates one.

## Baremetal

Machines that already exist. Nothing provisions them, so the file is a
description — and the `base` role **fails** when the description is wrong, which
is the point of writing it down.

```yaml
provider: baremetal
kind: baremetal
defaults:
  os: { distribution: debian, version: "13", timezone: Europe/London }
  ansible: { user: deploy, port: 22 }
```

Each host must carry `network.ipv4` (or a resolvable `fqdn`) — nothing can
discover a machine it did not create.

### Adopting a box

1. Install Debian 13 and make sure `python3` and `sudo` are present.
2. Create the deploy user and install the CI public key:

   ```bash
   sudo adduser --disabled-password --gecos "" deploy
   sudo usermod -aG sudo deploy
   sudo install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
   echo 'ssh-ed25519 AAAA... ci@provision' | \
     sudo tee /home/deploy/.ssh/authorized_keys
   sudo chown deploy:deploy /home/deploy/.ssh/authorized_keys
   sudo chmod 600 /home/deploy/.ssh/authorized_keys
   echo 'deploy ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/90-deploy
   ```

3. Write `hosts/baremetal/<hostname>.yml` describing what the machine really is.
4. Check the description before merging:

   ```bash
   cd ansible && ansible-playbook site.yml --limit <hostname> --check --diff
   ```

   A hardware assertion failure here means the file is wrong. Fix the file.

Use `format: none` on any disk holding a pre-existing filesystem — a ZFS pool,
an encrypted volume, someone's data. The `storage` role skips those entirely.

## Adding a provider folder

1. `mkdir hosts/<name>/` and write `_config.yml` with `provider: <name>` (it
   must equal the folder name) and a `kind`.
2. Add the credential keys to Bitwarden.
3. Add host files.

`scripts/list_stacks.py --provisioned` will pick the folder up, and every
pipeline iterates that list — there is no separate registry to update.

Disabling a whole folder:

```yaml
enabled: false
```

Its hosts leave the inventory and every pipeline's scope, without deleting
anything.
