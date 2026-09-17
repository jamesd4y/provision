# The options, and why this repo picked what it did

Each layer here had several credible answers. This records what they were, what
they cost, and what would make you switch — so the next person changing this
repo argues with a decision rather than rediscovering it.

---

## 1. Where servers come from

**Chosen: OpenTofu, one stack per provider kind, one folder per provider account
or node.**

`hosts/hetzner/` and `hosts/pve01/` each carry a `_config.yml` describing how to
reach that provider, and `{hostname}.yml` files describing the machines. The
`cpu` / `memory` / `storage` in a host file are inputs, not documentation: the
Hetzner stack picks the cheapest server type that satisfies them, and the
Proxmox stack sizes the VM directly.

`hosts/baremetal/` has the same shape but `kind: baremetal`, and no stack at
all — those machines already exist. Ansible and the deploy stage treat them
identically to a VM, which is the whole point of describing them the same way.

| Option | Why you'd want it | Why not here |
|---|---|---|
| **OpenTofu / Terraform** *(chosen)* | Real create/destroy lifecycle, a plan you can review before merge, one language across Hetzner, Proxmox and Cloudflare | State has to live somewhere and be locked; `prevent_destroy` guards are mandatory or a typo deletes a database |
| Provider CLI scripts (`hcloud`, `qm`) | Nothing to install, nothing to learn | No plan, no drift detection, no record of what exists. Every script becomes a one-way door |
| Pulumi | Real language, real types, real tests | A second runtime in the pipeline, and the Proxmox story is thinner |
| Crossplane / Cluster API | Excellent if Kubernetes is already the control plane | There is no Kubernetes here, and adding one to provision four VMs is absurd |
| Packer + immutable images | Fast, identical boots; rebuild instead of converge | Needs an image build pipeline and an image registry before the first server exists. Worth revisiting once host setup takes more than a couple of minutes |

**Switch if:** hosts start numbering in the dozens and Ansible runs get slow —
then bake the base role into a Packer image and let cloud-init do less.

---

## 2. How a host gets set up

**Chosen: Ansible over SSH, roles named per host in `ansible.roles`.**

The inventory is generated from `hosts/` by `scripts/inventory.py`, so there is
no inventory file to keep in sync. Each host lists its own roles, in order.

The `base` role does something slightly unusual: it compares the declared
`hardware` against gathered facts and **fails** on a baremetal host that does not
match. A file claiming 32 GiB on a box with 16 is a lie that will otherwise be
discovered by an OOM kill at 3am.

| Option | Why you'd want it | Why not here |
|---|---|---|
| **Ansible** *(chosen)* | Agentless, idempotent, converges existing boxes as happily as fresh ones, enormous module ecosystem | Slow over many hosts; YAML-as-a-language has sharp edges |
| cloud-init only | No day-2 tooling at all | Cannot converge an existing machine. A config change means rebuilding the host, which baremetal cannot do. Used here, but only to get a host far enough for Ansible to take over |
| NixOS + colmena / deploy-rs | Genuinely declarative, atomic rollbacks, strongest guarantees on the list | The whole repo becomes Nix, and the Compose layer becomes awkward. A big bet to make before the second host exists |
| Shell scripts over SSH | Zero dependencies, trivially readable | You write and re-write idempotency by hand, forever |
| Salt / Chef / Puppet | Fine tools, strong at scale | All want an agent or a master; more infrastructure than the infrastructure |

**Switch if:** the fleet outgrows roughly a dozen hosts, or convergence time
starts gating deploys — pull the stable parts into an image (see above) and keep
Ansible for the parts that genuinely change.

---

## 3. How containers reach a host

**Chosen: render the stack in CI, push it over SSH, `docker compose up`.**

`scripts/render_stack.py` turns `services/<name>.yml` plus the host's binding
into a directory: the Compose file verbatim, a generated override with the
Traefik labels, a `.env` of non-secret values, and a `secrets.map` of key names.
`scripts/deploy.sh` resolves the secrets, snapshots the remote directory, rsyncs,
brings the stack up with `--wait`, and rolls back to the snapshot if that fails.

No agent runs on the host. The host needs Docker, a deploy user, and an SSH
port — nothing else.

| Option | Why you'd want it | Why not here |
|---|---|---|
| **Compose pushed over SSH** *(chosen)* | No agent, no daemon exposure, the deployed files are right there when you SSH in to debug, trivially scriptable rollback | CI needs SSH reach to every host; a deploy is a push, so a host that was down misses it until the next run |
| `docker --context` / remote daemon | Compose files never land on the host | Exposing the daemon socket — even over SSH — is handing out root. And debugging is worse: nothing on the host says what it is running |
| GitOps agent (Komodo, Dockge, Watchtower) | Self-healing, pulls instead of receiving, gives you a UI | One more moving part per host to install, update and secure. Genuinely better once hosts are frequently offline or numerous |
| Ansible's `docker_compose_v2` | One tool for setup and services | Couples every container rollout to a full Ansible run, and gives up per-stack health gating and rollback |
| Kubernetes / Nomad | Real scheduling, real rollouts, real service discovery | Vastly more machinery than a handful of hosts running a handful of stacks. This is the answer when placement stops being a line in a YAML file |

**Switch if:** hosts start going offline and coming back, or you want a host to
self-heal without CI — that is exactly what a pull-based agent is for.

---

## 4. Where secrets live

**Chosen: Bitwarden Secrets Manager, referenced by key from the repo.**

The repo contains `GITEA_DB_PASSWORD: gitea/db_password`. The value is fetched
by `scripts/secrets.sh` at the moment of use, written mode 0600 into an
ephemeral container, and shredded when the step ends. Woodpecker holds one
secret — the machine-account token.

| Option | Why you'd want it | Why not here |
|---|---|---|
| **Bitwarden Secrets Manager** *(chosen)* | Simple model, machine accounts with per-project scope, works with an account you likely already have | No dynamic secrets, no automatic rotation, no lease/revoke. Rotation is a human doing it |
| SOPS + age, encrypted in-repo | Versioned with the code, reviewable diffs, works offline, no service to run | The encrypted blobs are still distributed to everyone with repo access; revoking someone means rotating everything |
| Vault / OpenBao | Dynamic credentials, leases, revocation, audit log — the strongest story here | You have to run and secure it first, and it is the thing everything else depends on. Chicken and egg for a fresh repo |
| Woodpecker secrets only | Nothing extra to run | Invisible to the repo, no audit trail, no way to restore them, and they leak into every pipeline that can see them |
| 1Password / Doppler | Good UX, low ops burden | A paid external dependency sitting in the critical path of every deploy |

**Switch if:** you need credentials that rotate on their own, or an audit trail
of who read what — that is Vault/OpenBao, and by then running it is justified.

---

## 5. What terminates TLS

**Chosen: Traefik per host, routed by labels generated from the host file.**

Nothing in `services/*.yml` mentions a domain. The host binding does, and
`render_stack.py` emits the router labels into a Compose override. Certificates
come from Let's Encrypt over the Cloudflare DNS-01 challenge, so a host behind
NAT gets real certificates without exposing port 80.

| Option | Why you'd want it | Why not here |
|---|---|---|
| **Traefik + labels** *(chosen)* | Zero-touch discovery; adding a domain is one line in a host file | Labels are invisible until the container runs; a bad label fails quietly. Mitigated here by generating them rather than hand-writing them |
| Caddy + generated Caddyfile | Explicit, greppable config; automatic HTTPS | Routing changes need the proxy redeployed, not just the app |
| Cloudflare Tunnel, no local proxy | Nothing listens on 80/443 at all; works behind CGNAT | No LAN-local path to services, and every byte egresses through Cloudflare. Excellent complement, poor sole answer |
| nginx-proxy + acme-companion | Battle-tested, familiar | Less active, and DNS-01 support is fiddlier |

**Switch if:** you add hosts with no inbound connectivity at all — Cloudflare
Tunnels alongside Traefik is the natural next step, and `cloudflare/zones.yml`
already has the token scoping for it.

---

## 6. How logs get off a host

**Chosen: Vector on every host, reading the Docker API, shipping to VictoriaLogs.**

Docker keeps writing `json-file` locally. `docs/logging.md` has the full
reasoning; the short version is that Docker's remote log drivers have no durable
buffer, block container stdout by default when the log server is unwell, and —
because dual logging keeps the local cache anyway — do not even save you the
local write.

| Option | Why you'd want it | Why not here |
|---|---|---|
| **Vector** *(chosen)* | Disk buffer with retry, VRL for parsing and redaction, reads container labels so stack/service enrich themselves, one agent that can later take journald and file logs | ~50–80 MiB per host, and VRL is another small language to learn |
| Docker `syslog` log-driver, direct | No extra container at all | Blocking by default; non-blocking drops silently; flattens structured logs into RFC5424. Supported here as `docker_log_driver` for hosts too small to justify a collector |
| Fluent Bit | Much smaller footprint (~5 MiB), fine `http` output to the jsonline endpoint | Weaker transform language; worth switching to if a host is genuinely memory-constrained |
| OpenTelemetry Collector | Vendor-neutral, one agent for logs, metrics and traces | Heavier, and the filelog receiver config is considerably more verbose for the same result |
| Promtail / Grafana Alloy | VictoriaLogs accepts the Loki push protocol | Promtail is superseded; no reason to start there now |
| `vlagent` | VictoriaLogs' own forwarder — good for buffering toward a central store | It forwards, it does not collect from Docker; it complements a collector rather than replacing one |

**Switch if:** a host cannot spare the memory (Fluent Bit), or you start
collecting traces too and want one agent for everything (OpenTelemetry).

---

## 7. Which CI

**Given: Woodpecker.**

It suits this shape of work well: workflows are separate files with explicit
`depends_on`, so provision → dns → configure → deploy is expressed directly
rather than as one long job. Each workflow gets its own clone, which is why
`scripts/refresh_host_ips.sh` exists — later stages reconstruct addresses from
OpenTofu state rather than passing artifacts along.

The pipelines call `scripts/*` and almost nothing else. Moving to another CI
system would mean rewriting eight small YAML files, not rewriting the repo.
