# Tailscale

Every host joins a tailnet, and that is how everything reaches them. No host has
a public SSH rule — not at the cloud firewall, not in nftables.

```
   Woodpecker runner                 hosts/
   (ephemeral, tag:ci) ──┐        ┌── web01   tag:server   Hetzner
                         ├─ tailnet ── db01    tag:server   Hetzner, no public v4
   you, on a laptop  ────┘        ├── media01 tag:server   Proxmox, behind NAT
                                  └── nas01   tag:server   baremetal, behind NAT
```

What this bought:

- **One log store.** `media01` and `nas01` are on a different network from
  `db01` and were never routable to it. On the tailnet they are, so the two
  VictoriaLogs instances collapsed into one — see [logging.md](logging.md).
- **CI reaches everything.** The runner has no fixed egress IP, so a firewall
  rule could never have allowed it without allowing the internet.
- **NAT stops mattering.** The Proxmox VM and the baremetal box are deployable
  without a single port forward.
- **Nothing listens on 22.** The one port every scanner on the internet tries
  is simply not open.

## What is in the repo and what is not

Membership is in the repo. Access policy is not: ACLs, tag owners and grants
live in the Tailscale admin console. The repo holds `tailscale/tailnet.yml`
(which tailnet, which MagicDNS suffix, which Bitwarden keys, which tags) and a
`tailscale:` block per host.

That split means the console and the repo have to agree, so the exact policy
this repo needs is written out below. If you later want it version-controlled,
the `tailscale/tailscale` OpenTofu provider has an `acl` resource and would slot
in as `tofu/tailscale/` alongside `tofu/cloudflare/`.

## One-time setup

### 1. Tag owners and ACL

In the admin console's **Access controls**, you need three things. Tags first —
note `tag:provision` exists only so a single OAuth client can mint keys for both
of the other tags, which is the arrangement Tailscale documents for
multi-tag clients:

```jsonc
{
  "tagOwners": {
    "tag:provision": ["autogroup:admin"],
    "tag:server":    ["tag:provision"],
    "tag:ci":        ["tag:provision"],
  },

  "acls": [
    // You reach everything.
    { "action": "accept", "src": ["autogroup:admin"], "dst": ["tag:server:*"] },

    // CI reaches hosts on SSH, and nothing else. A compromised runner cannot
    // read the log store or talk to Postgres.
    { "action": "accept", "src": ["tag:ci"], "dst": ["tag:server:22"] },

    // Hosts ship logs to whichever host runs VictoriaLogs.
    { "action": "accept", "src": ["tag:server"], "dst": ["tag:server:9428"] },
  ],
}
```

Keep it this tight. Every host is `tag:server`, so a broader rule would let any
compromised container reach every other host on every port.

### 2. OAuth client

**Trust credentials → OAuth**, with:

- scope: `auth_keys` (write)
- tag: `tag:provision`

Store the two halves in Bitwarden under the keys named in
`tailscale/tailnet.yml`:

| Bitwarden key | Value |
|---|---|
| `tailscale/oauth_client_id` | the client ID |
| `tailscale/oauth_client_secret` | the client secret |

Nothing else is needed. `scripts/tailscale_authkey.sh` exchanges these for
short-lived keys whenever one is required.

### 3. Fill in `tailscale/tailnet.yml`

```yaml
tailnet: example.com
magic_dns_suffix: tail1a2b3c.ts.net    # admin console → DNS
oauth_client_id: tailscale/oauth_client_id
oauth_client_secret: tailscale/oauth_client_secret
```

The MagicDNS suffix is load-bearing: `ansible_host` becomes
`<hostname>.<suffix>`, so a wrong value means nothing can be reached. Confirm
it with `tailscale status --json | jq -r .MagicDNSSuffix` from any node.

## How a host joins

Two paths, and the first one is why a host with no public SSH is reachable at
all:

**At first boot.** `scripts/tofu.sh` mints a reusable, pre-authorised,
`tag:server` key that expires in an hour and passes it to the provisioning
stack. cloud-init installs Tailscale and runs `tailscale up` before anything
else needs to reach the machine.

**On every Ansible run.** The `tailscale` role is idempotent: it reads
`tailscale status` first and only registers a host that is not already
`Running`. A host that is fine costs one status call. A host that somehow lost
its registration re-registers, provided the run carries a key.

The role runs **before** `docker` and `firewall`, because the firewall role is
what closes the public path, and it does so over the connection the tailnet is
providing.

The bootstrap key is visible in `/var/lib/cloud` on the host and in the
provider's user-data until it expires. That is why it is a minted key and not
the OAuth client secret: worst case, someone who can already read your Hetzner
API tokens gets a key that can add a `tag:server` node for the next hour.

## How CI joins

`scripts/tailscale_up.sh` registers the runner as an **ephemeral** `tag:ci`
node, so Tailscale removes it shortly after the step ends rather than leaving a
dead device per pipeline.

It picks its networking mode automatically:

| Mode | When | How SSH gets through |
|---|---|---|
| kernel | `/dev/net/tun` exists | normal routing, nothing special |
| userspace | no TUN device — the usual unprivileged container | tailscaled runs a SOCKS5 proxy; `deploy.sh` and Ansible use it as a `ProxyCommand` |

In userspace mode the proxy also resolves MagicDNS names, so
`web01.tail1a2b3c.ts.net` works without the container's resolver knowing
anything about the tailnet. That is why `netcat-openbsd` is in the CI image:
`nc -X 5` is what carries SSH through the proxy.

Nothing in the pipeline needs a Tailscale secret of its own — the same
`bws_access_token` that unlocks everything else mints the CI key.

## Adding a host

Nothing to do. Provider defaults set `tailscale.enabled: true`, so a new host
file joins the tailnet at first boot and is reachable at
`<hostname>.<magic_dns_suffix>`.

Override per host only when you need to:

```yaml
tailscale:
  enabled: true
  hostname: web01          # defaults to the host's own name
  tags: [tag:server]       # must be a tag the OAuth client owns
  accept_dns: true
  accept_routes: false
  extra_args: []           # passed verbatim to `tailscale up`
```

Validation rejects a host that is on neither the tailnet nor public SSH, because
that host would be unreachable the moment the firewall role ran.

## Removing a host

Deleting the host file destroys the machine (and `prevent_destroy` will stop you
until you mean it). The tailnet device outlives it — `tag:server` nodes are not
ephemeral, deliberately, so a host rebooting does not vanish mid-deploy. Delete
the stale device in the admin console, or it will hold its MagicDNS name.

## If you get locked out

The honest risk of tailnet-only access: if `tailscaled` on a host is broken, the
public path you would normally use is not there. Recovery is the provider
console, and it works because `qemu-guest-agent` is installed at first boot.

**Hetzner**

1. Cloud console → the server → **Reset root password** (goes through the guest
   agent; note the password it shows you).
2. Open **Console**, log in as root.
3. `systemctl stop nftables` to get the public path back, then fix Tailscale:
   `tailscale up --auth-key=... --advertise-tags=tag:server`.
4. Re-run the configure pipeline, which puts the ruleset back.

**Proxmox**

1. `qm guest passwd <vmid> root` on the node, or use the VM console directly.
2. Same three steps as above.

**Reopening the public path deliberately**

```yaml
# hosts/<provider>/<hostname>.yml
network:
  public_ssh: true
```

That re-adds port 22 at both the cloud firewall and in nftables. Validation
warns about it every run so it does not become permanent by accident. The cloud
firewall half applies without touching the host, so it is worth doing before you
start console surgery.

## What this deliberately does not do

- **No subnet router.** Nothing advertises `10.20.0.0/24`, so LAN-only devices
  (the Proxmox host UI itself, switches, IPMI) are not reachable from the
  tailnet. Add `--advertise-routes` via `extra_args` on one homelab host and
  approve the route if you want that.
- **No exit node.** Nothing routes general internet traffic.
- **No Tailscale SSH.** `tailscaled` does not terminate SSH; sshd still does,
  authenticating with the deploy key, with the tailnet as transport only. This
  keeps key-based access working even if the tailnet policy is wrong, at the
  cost of not getting per-session SSH audit logs. Flip `tailscale.ssh: true` on
  a host to change that, and add an `ssh` block to the console policy.

## Checking it

```bash
# what the repo thinks the fleet looks like
python3 scripts/inventory.py --list | jq '._meta.hostvars | map_values(.ansible_host)'

# from a host
tailscale status
tailscale netcheck                       # is this node relaying or direct?
tailscale ping db01                      # direct or via DERP?

# from your laptop, once you are on the tailnet
ssh deploy@web01.tail1a2b3c.ts.net
```

`tailscale ping` reporting a DERP relay rather than a direct connection usually
means UDP 41641 is not reaching the host. That rule is derived automatically for
every tailnet host — check it survived into the provider firewall with
`jq '.hosts[].firewall_rules' tofu/hetzner/vars/hetzner.tfvars.json`.
