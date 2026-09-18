# Metrics

Host and container metrics go to VictoriaMetrics. A vmagent on each host scrapes
what is listening on loopback and remote-writes to the store.

```
  node_exporter   127.0.0.1:9100     host: cpu, memory, disk, network
  cadvisor        127.0.0.1:8081     per-container cpu, memory, io
  traefik         127.0.0.1:8082     requests, latency, TLS
  <stack>         127.0.0.1:<port>   whatever it exposes
        │
        ▼
  vmagent (host network, per host)   disk-buffered, retries
        │  remote_write
        ▼
  victoria-metrics on db01           reached over the tailnet
```

Same shape as the log pipeline, for the same reasons: a collector per host that
buffers to disk, and one store the whole fleet writes to.

## Nothing is reachable off the host

Every exporter here binds `127.0.0.1`, and vmagent runs on the host network so
it can reach them. That matters because none of them authenticate: cAdvisor
enumerates every container, image and mount; node_exporter describes the machine
in detail. On a tailnet it would be tempting to bind them to the tailnet
address and scrape centrally — don't. Loopback means a compromised container on
another host cannot read them either.

A test enforces it: any service declaring `metrics` must publish that port to a
loopback bind.

## Adding metrics to a service

Two lines in `services/<name>.yml`:

```yaml
x-provision:
  metrics:
    port: 9187          # published on 127.0.0.1:9187
    path: /metrics      # optional
    job: postgres       # optional; defaults to the stack name
```

and publish the port:

```yaml
    ports:
      - "127.0.0.1:9187:9187"
```

That is all. The scrape target list is generated per host from the stacks that
host runs, so nothing else needs editing — and a host that does not run the
stack never looks for it.

## How targets reach vmagent

`SCRAPE_TARGETS_JSON` is computed from the host's bindings and interpolated into
a Prometheus `file_sd` file. Look at what a host would scrape:

```bash
python3 scripts/render_stack.py --host web01 --service vmagent
cd build/deploy/web01/vmagent && docker compose config \
  | python3 -c "import sys,yaml,json;print(json.dumps(json.loads(yaml.safe_load(sys.stdin)['configs']['vmagent_targets']['content']),indent=2))"
```

Service discovery was the alternative — vmagent supports `docker_sd_configs`.
Generating the list instead means a missing target is a diff in a pull request
rather than something you notice weeks later when a dashboard is empty.

## Labels

Every series carries `host` (from vmagent's `external_labels`), `instance` (the
host, not a loopback address that would be identical everywhere) and `job`.
Container series from cAdvisor also carry the compose project and service.

cAdvisor's container labels are capped deliberately:
`--store_container_labels=false` plus an explicit whitelist. Without it, every
label this repo attaches to a container becomes a metric label, and the series
count grows with your label conventions rather than your infrastructure.

## Querying

```bash
# from anywhere on the tailnet
curl 'http://db01.tail1a2b3c.ts.net:8428/api/v1/query?query=up'

# which hosts are reporting at all
curl -s 'http://db01.tail1a2b3c.ts.net:8428/api/v1/query?query=count%20by%20(host)(up)'

# memory used per container on one host
curl -s 'http://db01.tail1a2b3c.ts.net:8428/api/v1/query' \
  --data-urlencode 'query=container_memory_usage_bytes{host="web01"}'
```

The built-in UI is at `/vmui`. It is on the tailnet, so no tunnel — and not
public, because the store binds only addresses the tailnet and private network
can reach.

## Retention and disk

13 months by default, so year-on-year comparisons work. VictoriaMetrics is
efficient enough that this is usually a few GiB for a fleet this size;
`VICTORIALOGS_MAX_DISK_BYTES`' equivalent, `VICTORIAMETRICS_MAX_DISK_BYTES`,
caps it and drops the oldest data rather than failing writes.

Metrics are deliberately **not** backed up — see
[backups.md](backups.md#what-is-deliberately-not-backed-up).

## When metrics stop arriving

1. `up` is the first query. `count by (host)(up)` shows which hosts report.
2. On the host: `docker logs vmagent`. Scrape errors are not suppressed
   (`-promscrape.suppressScrapeErrors=false`), so a target that is refusing
   connections says so.
3. `curl -s http://127.0.0.1:8429/api/v1/targets | jq '.data.activeTargets[] | {scrapeUrl, health, lastError}'`
   on the host shows what vmagent thinks of each target.
4. A growing `/srv/vmagent/data` means it cannot deliver — check the store is
   reachable from that host over the tailnet.
