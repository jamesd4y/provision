# The pipelines

Eight Woodpecker workflows in `.woodpecker/`. Each one is thin: it works out
scope, then calls a script from `scripts/` that also runs on a laptop.

```
                    push / PR
                        │
                   ┌────▼─────┐
                   │ validate │  lint, schema, cross-refs, tests, tofu validate
                   └────┬─────┘  no credentials, no network, no state
              PR ───────┼─────── default branch
           ┌────▼───┐   │   ┌────▼──────┐
           │  plan  │   │   │ provision │  OpenTofu apply per provider folder
           └────────┘   │   └────┬──────┘
        posts the diff  │   ┌────▼──┐
        on the PR       │   │  dns  │     Cloudflare records follow the hosts
                        │   └────┬──┘
                        │   ┌────▼──────┐
                        │   │ configure │  Ansible: users, Docker, firewall
                        │   └────┬──────┘
                        │   ┌────▼───┐
                        │   │ deploy │    compose stacks over SSH
                        │   └────────┘
```

| Workflow | Trigger | Does |
|---|---|---|
| `validate` | push, PR, manual | yamllint, `validate.py`, pytest, renders everything, `tofu fmt`/`validate`, credential scan |
| `plan` | PR | `tofu plan` for every stack the diff reaches; posts it as a PR comment |
| `provision` | push to default, manual | `tofu apply` per affected provider folder, then reports addresses |
| `dns` | push to default, manual | renders and applies the Cloudflare stack |
| `configure` | push to default, manual, cron | `ansible-playbook site.yml --limit <changed hosts>` |
| `deploy` | push to default, manual, cron | checks secrets resolve, then `deploy.sh` per host |
| `drift` | cron `drift`, manual | read-only: `plan -detailed-exitcode` and `ansible --check --diff` |
| `image` | push touching `ci/`, manual | rebuilds the toolbox image every other workflow runs in |

## Scope

`scripts/changed.py` turns a git diff into the smallest correct blast radius:

| Changed | Pulls in |
|---|---|
| `services/gitea.yml` | every host running `gitea` — that stack only |
| `hosts/hetzner/web01.yml` | `web01`, all of its stacks, its provider stack, DNS |
| `hosts/hetzner/_config.yml` | every host in that folder |
| `ansible/roles/firewall/**` | every host whose roles include `firewall` |
| `cloudflare/**` | DNS |
| `scripts/**`, `schemas/**`, `ansible/site.yml` | everything — the blast radius is no longer knowable, so assume the worst |

Check it before you push:

```bash
python3 scripts/changed.py --base origin/main | jq
```

A baremetal host never reaches a provisioning stage, however it changed.

## Secrets the pipelines need

| Woodpecker secret | Used by | Notes |
|---|---|---|
| `bws_access_token` | plan, provision, dns, configure, deploy, drift | the only real credential; everything else comes from Bitwarden |
| `bws_project_id` | same | scopes lookups to one project |
| `ghcr_username`, `ghcr_token` | image | pushing the toolbox image |
| `forge_token` | plan | posting the plan comment; optional — without it the plan stays in the step log |

Restrict all of them to `push`, `manual` and `cron`. A fork's pull request must
never be able to read them, which is also why `plan` holds read credentials only
and never applies.

## Workflows do not share a workspace

Each workflow gets a fresh clone, so nothing produced by `provision` is visible
to `deploy`. Rather than pass artifacts around, later stages reconstruct what
they need from OpenTofu state:

```bash
scripts/refresh_host_ips.sh    # hosts/ declarations + every provisioned stack's output
```

State is the authoritative copy of an address anyway, so this is more correct
than an artifact would be, not just simpler.

## State

`scripts/tofu.sh` handles init, credentials and state addressing:

```bash
scripts/tofu.sh hetzner hetzner plan
scripts/tofu.sh proxmox pve01 apply -auto-approve
scripts/tofu.sh cloudflare default plan
```

The arguments are `<stack> <workspace> <command…>`, where the workspace is the
`hosts/` folder. The HTTP backend has no named workspaces, so each pair gets its
own state address — `pve01` and `pve02` are independent, and one node being down
never blocks the other.

Each stack's credentials are fetched only when that stack runs, so a DNS apply
never holds a cloud token.

## The toolbox image

Every step runs in `ghcr.io/jamesd4y/provision-ci:latest`, built from
`ci/Dockerfile` with the repo root as context. It pins OpenTofu, `bws`,
Ansible and the Python tooling in one place, so a pipeline from six months ago
used exactly these versions and an apply never downloads a tool mid-run.

Bump a version in `ci/Dockerfile` or `ci/requirements.txt`, merge, and the
`image` workflow rebuilds it.

## Running a stage by hand

```bash
export BWS_ACCESS_TOKEN=... BWS_PROJECT_ID=...

scripts/refresh_host_ips.sh
eval "$(scripts/ssh_setup.sh)"

scripts/tofu.sh hetzner hetzner plan
cd ansible && ansible-playbook site.yml --limit web01 --check --diff
scripts/deploy.sh web01 gitea --dry-run
```

Every one of these is exactly what CI runs. If something surprises you in a
pipeline, it will surprise you the same way here.

## When something fails

- **validate** — reproduce with `python3 scripts/validate.py` and
  `python3 -m pytest tests/ -q`. Nothing was touched.
- **plan** shows a *replace* — you changed something immutable on an existing
  host. Read the plan before merging.
- **provision** fails a `precondition` — the error names the file and the field.
  `prevent_destroy` firing means a host file was deleted; see
  [adding-a-host.md](adding-a-host.md#taking-a-host-out-of-service).
- **deploy** rolls back — the stack failed its health check and the previous
  release was restored from `/opt/stacks/<name>.previous`. The host is serving
  the old version; fix forward.
- **drift** is red — the fleet no longer matches the repo. Read the plan, then
  either correct the repo or re-run `provision`/`configure` to reconcile.
