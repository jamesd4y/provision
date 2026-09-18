# Backups

restic, nightly, to TrueNAS. One repository per host, encrypted client-side,
deduplicated.

## Why restic

You asked for deduplication and a TrueNAS destination. restic chunks content and
stores each chunk once, so last night's Postgres dump and tonight's cost roughly
what changed between them rather than two full copies. It also encrypts on the
host before anything leaves it, which matters when the destination is a NAS that
other things can read.

**Do not enable ZFS deduplication on the dataset holding this.** restic has
already deduplicated the data, so ZFS would find almost nothing to match while
costing you RAM permanently. Dataset compression is fine and still helps a
little.

## Where it goes

```yaml
# backup/config.yml
repository:
  url: "sftp:restic@truenas.tail1a2b3c.ts.net:/mnt/tank/restic/{host}"
```

`{host}` is substituted per host. Separate repositories mean one host cannot
read or rewrite another's history, and a restore never has to filter someone
else's data. The cost is that chunks are not pooled across hosts — isolation
bought with some duplication.

SFTP needs nothing on TrueNAS beyond an SSH user, and it rides the tailnet, so
the NAS needs no public exposure. Two alternatives worth knowing:

| Destination | Why |
|---|---|
| `rest:https://truenas:8000/{host}/` | rest-server in **append-only** mode, so a compromised host can add snapshots but not delete them. The strongest option here, and worth moving to if ransomware is a concern |
| `s3:...` (MinIO on TrueNAS) | If you already run MinIO. More moving parts for no gain over SFTP on a LAN |

The dataset is still only one copy. TrueNAS snapshots and replication of that
dataset are what protect against *its* disk failing — restic protects against
the fleet's.

## What each stack does

Declared in the stack's own file, because that is where the knowledge lives:

```yaml
x-provision:
  backup:
    paths: [data]              # under the stack's data root; defaults to data_dirs
    exclude: [data/tmp]        # anchored to the data root
    pre: ["..."]               # runs before the snapshot
    post: ["..."]              # always runs, even on failure
    stop: false                # stop the stack for the duration
```

Hooks get `$STACK`, `$DATA_ROOT` and `$STAGING` exported. `$STAGING` is a
root-owned directory that is captured alongside the declared paths — it is where
a hook puts a consistent copy.

### A database is dumped, not copied

`services/postgres.yml` is the example worth reading:

```yaml
  backup:
    paths: []                  # explicitly NOT the live data directory
    pre:
      - docker exec -i postgres sh -c 'pg_dumpall -U "$POSTGRES_USER" ...' > "$STAGING/all.sql"
    post:
      - rm -f "$STAGING/all.sql"
```

Copying a live Postgres data directory gives you a torn page — a backup that
restores into a database that will not start, and you find out when you need it.
`pg_dumpall` runs against the running server, is consistent by construction, and
costs no downtime.

`paths: []` means *none of the data directory*, and is distinguished from
omitting `paths` entirely. A test pins that behaviour, because the failure mode
of getting it wrong is silent.

uptime-kuma does the same thing for SQLite: `.backup` produces a consistent
file, and the live `kuma.db`, `-wal` and `-shm` are excluded so the torn copy is
not captured alongside it.

### Opting out

A stack that stores data must say something. Opting out needs a reason:

```yaml
  backup:
    enabled: false
    reason: >-
      The only thing here is Vector's disk buffer — logs not yet delivered.
```

Validation rejects a stack with `data_dirs` and no `backup` block, and rejects
`enabled: false` with no reason. That is deliberate: the common way to lose data
is nobody ever deciding, rather than someone deciding wrongly.

## What is deliberately not backed up

| Stack | Why |
|---|---|
| victoria-logs | Logs are observability, not records. Restoring stale logs onto a running instance is worse than starting empty — raise retention instead |
| victoria-metrics | Same. If the history becomes worth keeping, use `vmbackup` against a snapshot; restic over a live TSDB directory does not restore |
| vector, vmagent | Nothing but undelivered queues, meaningless once restored |

## Schedule

| Timer | When | What |
|---|---|---|
| `provision-backup.timer` | nightly 02:30 (+45m jitter) | snapshot every stack, then apply retention |
| `provision-backup-prune.timer` | Sundays 05:00 | reclaim space from forgotten snapshots |
| `provision-backup-check.timer` | Sundays 06:00 | `restic check --read-data-subset=5%` |

The jitter matters: without it every host hits the NAS on the same second.
`Persistent=true` means a host that was off at 02:30 backs up when it returns.

Prune and check both declare `Conflicts=provision-backup.service`, because
pruning rewrites the repository and must never overlap a backup.

**The check reads data back.** `restic check` on its own only validates the
repository's structure; it would not notice the NAS returning corrupt blobs.
`--read-data-subset` actually fetches and verifies content. A backup nobody has
ever read is a hope, not a backup.

## Restoring

```bash
export RESTIC_PASSWORD=$(scripts/secrets.sh get backup/restic_password)
export RESTIC_REPOSITORY=sftp:restic@truenas.tail1a2b3c.ts.net:/mnt/tank/restic/web01

restic snapshots --tag stack:gitea          # what is there
restic restore latest --tag stack:gitea --target /tmp/restore
```

Postgres comes back from the dump:

```bash
restic restore latest --tag stack:postgres --target /tmp/restore
docker exec -i postgres psql -U postgres < /tmp/restore/var/lib/provision-backup/postgres/all.sql
```

Practise this before you need it. A restore you have never run is the same
category of thing as a backup you have never read.

## Setup

1. On TrueNAS: a dataset (`tank/restic`), a user (`restic`) with SSH access to
   it, and Tailscale on the NAS so hosts reach it over the tailnet.
2. Generate a repository password and store it in Bitwarden under
   `backup/restic_password`. **Keep a copy somewhere outside the fleet** — lose
   it and every backup is unreadable. There is no recovery path; that is what
   client-side encryption means.
3. Optionally store an SFTP private key under `backup/truenas_ssh_key`.
4. Add `backup` to the host's `ansible.roles`. Validation will tell you if a
   host has data worth keeping and no backup role.

The first configure run initialises the repository. `restic init` on an existing
repository is checked for rather than blindly attempted, so re-running is safe.

## Checking it is working

```bash
ssh deploy@web01.tail1a2b3c.ts.net
systemctl list-timers 'provision-backup*'
journalctl -u provision-backup.service -n 50
sudo systemctl start provision-backup.service    # run one now
```

Metrics are the better signal at fleet scale: alert on the age of the newest
snapshot rather than on a job failing, because a job that stops being scheduled
never fails.
