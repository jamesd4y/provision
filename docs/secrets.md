# Secrets

The repo stores secret **keys**. Bitwarden Secrets Manager stores the values.

```yaml
# hosts/hetzner/web01.yml
secrets:
  GITEA_DB_PASSWORD: gitea/db_password    # a key, resolved at deploy time
```

Nothing in `hosts/`, `services/` or `cloudflare/` ever contains a credential,
and the `no-secrets-committed` step in the validate pipeline fails the build if
something credential-shaped shows up there.

## Setup

1. Create a Secrets Manager **project**, e.g. `provision`.
2. Create a **machine account**, give it *read* access to that project, and
   generate an access token.
3. Add two Woodpecker secrets to the repository:

   | Woodpecker secret | Value |
   |---|---|
   | `bws_access_token` | the machine account access token |
   | `bws_project_id` | the project's UUID |

   Restrict both to the events the pipelines actually use (`push`, `manual`,
   `cron`). Do **not** expose them to `pull_request`: a fork's PR must never be
   able to read the fleet's credentials.

That token is the only credential Woodpecker holds. Everything else — cloud API
tokens, the SSH key, the state backend password, every service's database
password — hangs off it.

## Keys this repo expects

Keys are referenced from the YAML, so these are conventions, not hard-coded
names. Change the left column and the right column follows.

| Key | Used by | What it is |
|---|---|---|
| `hetzner/api_token` | `tofu/hetzner` | Hetzner Cloud API token, read+write |
| `proxmox/<node>_api_token` | `tofu/proxmox` | `user@realm!tokenid=uuid` |
| `proxmox/<node>_root_password` | `tofu/proxmox` | SSH password for snippet uploads |
| `cloudflare/dns_api_token` | `tofu/cloudflare` | Zone:Read + DNS:Edit on the managed zones |
| `cloudflare/account_id` | `cloudflare/zones.yml` | account id |
| `cloudflare/acme_dns_token` | `services/traefik.yml` | DNS:Edit, for the ACME DNS-01 challenge |
| `tofu/state_url` | `scripts/tofu.sh` | base URL of the HTTP state backend |
| `tofu/state_username`, `tofu/state_password` | `scripts/tofu.sh` | state backend credentials |
| `ssh/ci_private_key` | `scripts/ssh_setup.sh` | the deploy identity |
| `ssh/known_hosts` | `scripts/ssh_setup.sh` | pinned host keys (optional, recommended) |
| `<service>/<name>` | host bindings | whatever a stack needs |

Use two ACME/DNS tokens rather than one: the Traefik token lives on every edge
host and only needs `DNS:Edit`, while the OpenTofu token lives only in CI. A
compromised host should not be able to rewrite your zone wholesale.

## How resolution works

`scripts/secrets.sh` wraps the `bws` CLI:

```bash
scripts/secrets.sh get gitea/db_password                   # one value on stdout
scripts/secrets.sh check build/.../secrets.map             # do all keys resolve?
scripts/secrets.sh render build/.../secrets.map .../.env   # append VAR=value
eval "$(scripts/secrets.sh export cloudflare/dns_api_token CF_API_TOKEN)"
```

It makes **one** API call per invocation and answers every lookup from that
cached list, keeping a pipeline that resolves dozens of keys well inside the
rate limit. The cache is `mktemp` with `umask 077` and is shredded on exit,
including on interrupt.

Three things it refuses to do, each because the failure would be silent:

- a key that matches **nothing** fails the step rather than yielding an empty string
- a key that matches **more than one** secret fails, rather than picking one
- a value containing a **newline** fails, because `.env` cannot represent it and
  the remainder would be parsed as another variable

The deploy pipeline runs `secrets.sh check` before it touches any host, so a
missing secret stops the rollout before anything changes.

## Where values end up

| Place | Contents | Lifetime |
|---|---|---|
| `build/deploy/<host>/<stack>/secrets.map` | key **names** | the pipeline run |
| `build/deploy/<host>/<stack>/.env` | resolved values, mode 0600 | the pipeline run |
| `/opt/stacks/<stack>/.env` on the host | resolved values, mode 0600, owned by the deploy user | until the next deploy |
| Container environment | whatever the Compose file passes through | while it runs |
| `TF_VAR_*` in `scripts/tofu.sh` | provider credentials | that one OpenTofu command |

Values reach a host because a container needs them. They never reach the repo,
the Woodpecker UI, or a log — `secrets.sh check` prints key names and nothing
else, and OpenTofu's credential variables are all marked `sensitive`.

## The SSH identity

`scripts/ssh_setup.sh` writes the CI key from `ssh/ci_private_key` into the
ephemeral container at mode 0600 and exports the env vars Ansible and
`deploy.sh` need.

If `ssh/known_hosts` exists, host keys are **pinned** and a mismatch aborts the
connection. Without it, the scripts fall back to trust-on-first-use and say so
in the log. Pin them: collect the keys once after provisioning
(`ssh-keyscan -H web01.example.com db01.example.com ...`) and store the result.

## Rotating

1. Change the value in Bitwarden.
2. Re-run the pipeline that uses it — `deploy` for a service credential,
   `provision` for a cloud token.

No commit is involved, because the repo never held the value. Rotating the
*machine account token* means updating the `bws_access_token` Woodpecker secret,
and that is the one rotation the fleet cannot perform on its own.
