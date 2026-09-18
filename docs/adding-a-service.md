# Adding a service

A service is one Docker Compose file in `services/`, plus one `x-provision`
block. It contains no hostname, no domain and no secret — those belong to
whichever host runs it, which is what lets the same file run on a Hetzner VM and
a Proxmox VM unchanged.

## 1. Write `services/<name>.yml`

```yaml
x-provision:
  description: What this is, in one line.
  ingress: traefik          # traefik | host-port | none
  port: 3000                # the container port Traefik routes to
  singleton: true           # refuse to run in two places
  requires: [traefik]       # must be on the same host; deployed first
  required_env: [APP_SECRET_KEY]
  optional_env:
    APP_LOG_LEVEL: info     # default, overridable per host
  data_dirs: [data]         # created 0750 under the volume root before compose up
  healthcheck_url: http://localhost:3000/healthz

services:
  app:
    image: ghcr.io/example/app:1.4.2
    restart: unless-stopped
    environment:
      APP_SECRET_KEY: ${APP_SECRET_KEY}
      APP_LOG_LEVEL: ${APP_LOG_LEVEL}
      TZ: ${TZ}
    volumes:
      - ${DATA_ROOT}/data:/var/lib/app
    networks: [edge]
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:3000/healthz"]
      interval: 30s
      timeout: 5s
      retries: 5

networks:
  edge:
    external: true
```

Variables always available in the Compose file:

| Variable | Value |
|---|---|
| `STACK_NAME` | the stack's name |
| `HOSTNAME` | the host it is running on |
| `DATA_ROOT` | `/srv/<name>`, or the host's `volumes_root` |
| `TZ` | the host's timezone |
| `DOMAIN` | the first domain the host bound, if any |
| `DOMAINS` | all of them, comma-separated |

Plus everything in `optional_env`, everything in the host's `env:`, and every
key in the host's `secrets:`.

### ingress

- `traefik` — routing labels are **generated** into `docker-compose.override.yml`
  from the host's `domains`, and the stack is attached to the `edge` network.
  Do not write Traefik labels by hand; they would be overwritten.
- `host-port` — the stack publishes its own ports (this is what `traefik.yml`
  itself uses).
- `none` — internal only. Binding a domain to it is a validation error.

Pin image tags. `:latest` makes a redeploy non-reproducible and a rollback
meaningless.

## 2. Bind it to a host

In `hosts/<provider>/<hostname>.yml`:

```yaml
services:
  - name: app
    domains:
      - name: app.example.com
        proxied: true          # orange cloud; false for grey
    env:
      APP_LOG_LEVEL: debug
    secrets:
      APP_SECRET_KEY: app/secret_key   # a Bitwarden key, never a value
```

That single binding does four things: it creates the Cloudflare record, it
generates the Traefik router, it writes the `.env`, and it puts the host in the
deploy scope for this stack.

For a stack on several hosts, repeat the binding in each host file. Set
`singleton: true` if that would be a mistake, and validation will catch it.

## 3. Add the secrets to Bitwarden

Create a secret whose **key** is exactly the string in `secrets:`
(`app/secret_key`) in the project the CI machine account can read. See
[secrets.md](secrets.md).

## 4. Check it

```bash
python3 scripts/validate.py
python3 scripts/render_stack.py --host web01 --service app
cat build/deploy/web01/app/docker-compose.override.yml   # the generated labels
cat build/deploy/web01/app/.env                          # no secret values here
cat build/deploy/web01/app/secrets.map                    # key names only
```

With a Bitwarden token in your environment:

```bash
scripts/secrets.sh check build/deploy/web01/app/secrets.map   # do the keys resolve?
scripts/deploy.sh web01 app --dry-run
```

## What a deploy actually does

For each stack, in `requires` order:

1. render the bundle from `services/` and the host's binding
2. resolve secrets into `.env` (mode 0600)
3. snapshot the remote directory to `<path>.previous`
4. create the data dirs, rsync the bundle to `/opt/stacks/<name>`
5. `docker compose up -d --remove-orphans --wait`
6. on failure, restore the snapshot and bring the old stack back up, then fail
   the pipeline

Because the previous release stays on the host, a failed rollout is undone
without waiting for another CI round trip.

## Removing a service

Drop the binding from the host file. The next deploy leaves the containers
running — `compose down` is deliberately not automatic, because an accidental
deletion should not take a database with it. Stop it yourself when you are sure:

```bash
ssh deploy@web01 'cd /opt/stacks/app && docker compose down'
```

Then delete `services/app.yml` once no host references it.
