# Logging

Container logs go to VictoriaLogs. Docker keeps writing them locally with
`json-file`, and a Vector container on each host reads the Docker API, enriches
each line, and ships it.

```
  container stdout
        │
        ▼
  docker json-file        `docker logs` keeps working; rotated at 20m x 5
        │
        ▼
  vector (per host)       docker_logs source -> enrich -> disk buffer
        │                 host / stack / service attached to every line
        ▼
  victoria-logs           one for the fleet, tailnet-only, LogsQL over HTTP
```

## Why not just point Docker's log driver at VictoriaLogs

This was the obvious first idea and it is the wrong one, for three reasons that
survive checking:

**A remote driver has no durable buffer.** `log-driver: syslog` in its default
`mode: blocking` applies backpressure to container stdout when the log server is
slow or unreachable — your application stalls because your *logging* broke.
`mode: non-blocking` swaps that for a small in-memory ring buffer that silently
drops once full. Vector buffers to disk (512 MiB by default here) and retries,
so a VictoriaLogs restart costs you nothing.

**It doesn't even save the local write.** Docker's
[dual logging](https://docs.docker.com/engine/logging/dual-logging/) keeps a
local cache via the `local` driver so `docker logs` still works with a remote
driver. So you pay for the local copy either way; a remote driver only changes
where the *second* copy goes.

**VictoriaLogs speaks no driver protocol except syslog.** There is no
VictoriaLogs log driver, and no fluentd or GELF endpoint. Going direct means
flattening every line into RFC5424, so a JSON application log arrives as one
opaque message string and you lose the per-field querying that is the main
reason to run VictoriaLogs at all.

What you get for the extra container: structured fields, per-host enrichment,
disk buffering, and one collector that can later pick up journald, file logs or
metrics without touching the Docker daemon.

## Using the syslog driver anyway

It is a reasonable trade on a host too small to justify another container, and
it is supported — the driver is a variable, not a hard-coded string:

```yaml
# hosts/<provider>/<hostname>.yml
ansible:
  vars:
    docker_log_driver: syslog
    docker_log_opts:
      syslog-address: tcp://10.10.0.20:514
      syslog-format: rfc5424micro
      tag: "{{ '{{.Name}}' }}"
      mode: non-blocking          # never leave this blocking
      max-buffer-size: 4m
```

and give that host's VictoriaLogs a syslog listener, which is off by default:

```yaml
# on the host running victoria-logs
services:
  - name: victoria-logs
    env:
      VICTORIALOGS_SYSLOG_TCP: ":514"        # empty (the default) = no listener
      VICTORIALOGS_SYSLOG_BIND: 10.10.0.20   # never 0.0.0.0
```

and open the port in that host's `network.firewall`, scoped to the private
network, exactly as 9428 already is.

Drop the `vector` binding from that host if you do — running both means every
line is stored twice.

## What lands in VictoriaLogs

Each line carries three stream fields and keeps everything else as regular
fields:

| Field | From | Example |
|---|---|---|
| `host` | the host's name in `hosts/` | `web01` |
| `stack` | the Compose project, i.e. the `services/` name | `gitea` |
| `service` | the Compose service within the stack | `gitea` |
| `_msg` | the log line | `Starting server...` |
| `container_name`, `stream`, `origin` | plain fields, queryable, not streams | `gitea`, `stdout` |

Containers not started by Compose — `node-exporter`, which the Ansible role runs
directly — land under `stack: unmanaged` with `service` from the container name.

**Stream fields must stay low cardinality.** A stream is the unit VictoriaLogs
indexes by; putting `container_id` in there would create a new stream on every
container restart and wreck ingestion. `tests/test_logging.py` fails the build
if that set changes.

## Querying

```bash
# everything from one host in the last 15 minutes
curl http://db01.tail1a2b3c.ts.net:9428/select/logsql/query \
  -d 'query=host:web01 AND _time:15m'

# errors from one stack
curl http://db01.tail1a2b3c.ts.net:9428/select/logsql/query \
  -d 'query=stack:gitea AND error AND _time:1h'

# which streams exist at all
curl http://db01.tail1a2b3c.ts.net:9428/select/logsql/streams -d 'query=_time:1d'
```

The built-in UI is at `/select/vmui`. On the tailnet, that is simply
`http://db01.tail1a2b3c.ts.net:9428/select/vmui` from any device on the
tailnet — no tunnel, and no public exposure either.

## VictoriaLogs has no authentication

Anyone who can reach port 9428 can read every log line and delete data through
the HTTP API. This repo defends that in three places, and all three are tested:

- `VICTORIALOGS_BIND` defaults to `127.0.0.1`; a host that serves other hosts
  widens it to its **private** address, never `0.0.0.0`
- the host's `network.firewall` scopes 9428 to the private network, and the
  tailnet ACL restricts `tag:server:9428` to other hosts
- `services/victoria-logs.yml` is `ingress: none`, so a domain cannot be bound
  to it by accident

To publish the UI, put an authenticating layer in front — a Traefik basic-auth
middleware, or Cloudflare Access on the route. Do not simply bind it wider.

## One store for the whole fleet

`VICTORIALOGS_ENDPOINT` points every collector at `db01`, by its MagicDNS name:

```yaml
VICTORIALOGS_ENDPOINT: http://db01.tail1a2b3c.ts.net:9428
```

This used to be two stores. The Hetzner private network and the homelab LAN were
never routed to each other, so `media01` and `nas01` had nowhere to ship to but
a second VictoriaLogs on `nas01`. The tailnet made them one network and the
second store stopped earning its keep — see [tailscale.md](tailscale.md).

Two tests hold the line: every endpoint must resolve to a host this repo
actually runs the store on, and all collectors must agree on one endpoint. A
typo or a decommissioned store fails CI rather than quietly black-holing a
host's logs.

### Why the store binds every interface

`db01` sets `VICTORIALOGS_BIND: 0.0.0.0`, which looks alarming for a service
with no authentication. It is safe *on that host specifically*: `db01` has
`ipv4_enabled: false`, so there is no public IPv4 to bind to — every interface
means the Hetzner private network and the tailnet.

That pairing is enforced, not assumed: a test fails if any host binds the store
to `0.0.0.0` while having a public IPv4. On a host that does have one, use
Tailscale Serve (`tailscale serve --bg --tcp 9428 tcp://127.0.0.1:9428`) and
leave the bind on loopback instead.

## Adding journald

Host logs — sshd, systemd, nftables, unattended-upgrades — are not collected by
default. The Vector Debian image ships `journalctl` for exactly this, so adding
them is a source, a transform and two mounts in `services/vector.yml`:

```yaml
      sources:
        system:
          type: journald
          current_boot_only: true

      transforms:
        system_logs:
          type: remap
          inputs: [system]
          source: |
            .host = "${HOSTNAME}"
            .origin = "system"
            .stack = "system"
            .service = string(._SYSTEMD_UNIT) ?? string(.unit) ?? "journald"
```

add `system_logs` to the sink's `inputs`, and mount the journal:

```yaml
      - /var/log/journal:/var/log/journal:ro
      - /run/log/journal:/run/log/journal:ro
      - /etc/machine-id:/etc/machine-id:ro
```

This is left out of the default because journal access varies with how a host
stores its journal, and a source that fails to start takes the whole collector
down with it — including container logs that were working. Add it to one host,
confirm `origin:system` lines arrive, then roll it out.

## Changing versions

Both images are pinned. Bump them in `services/vector.yml` and
`services/victoria-logs.yml`, and check the Vector config still validates:

```bash
python3 scripts/render_stack.py --host web01 --service vector
cd build/deploy/web01/vector && docker compose config   # resolves ${...}
```

With a `vector` binary to hand, the resolved config can be checked properly:

```bash
docker compose config | yq '.configs.vector_config.content' > /tmp/v.yaml
vector validate --no-environment /tmp/v.yaml
```

## When logs stop arriving

1. **Is the collector running?** `ssh deploy@<host> 'docker ps | grep vector'`
2. **What is it complaining about?** `docker logs vector` — it is excluded from
   its own pipeline, so this is the only place its errors appear.
3. **Is the store reachable from that host?**
   `curl -sS http://<store>:9428/health` — a refused connection is usually the
   firewall rule, a timeout usually the bind address.
4. **Is the buffer filling?** `du -sh /srv/vector/data` on the host. Growing
   means Vector cannot deliver; at `VECTOR_BUFFER_BYTES` it stops reading and
   older container logs age out of `json-file` unshipped.
5. **Is the store full?** `-retention.maxDiskSpaceUsageBytes` drops the oldest
   data rather than failing writes, so silent gaps at the *old* end of a query
   mean retention, not a broken pipeline.
