# Cloudflare

DNS is derived, not written. Every record this repo manages comes from a host
file or a service binding, so a record can never point at a machine that moved.

## Declaring a zone

A zone must be listed in `cloudflare/zones.yml` before anything may write to it.
A domain outside the list is a validation error, not a surprise record in
someone else's zone.

```yaml
account_id: cloudflare/account_id      # a Bitwarden key, not a value
api_token: cloudflare/dns_api_token

defaults:
  ttl: 1                # 1 means "automatic"
  proxied: true         # service records are orange-clouded by default
  comment_prefix: managed-by:provision

zones:
  example.com:
    zone_id: 0123456789abcdef0123456789abcdef
    records:            # zone-level records not tied to any host
      - { name: "@", type: MX, value: route1.mx.cloudflare.net, priority: 10 }
      - { name: _dmarc, type: TXT, value: "v=DMARC1; p=reject; rua=mailto:dmarc@example.com" }
```

## Where records come from

**Host records** — from a host's `dns:` block:

```yaml
dns:
  zone: example.com
  record: web01         # null means "no public record"
  proxied: false        # grey cloud, so SSH and DNS-01 keep working
  extra_records:
    - { name: "@", type: CAA, value: '0 issue "letsencrypt.org"' }
```

A host gets an `A` and an `AAAA` for whichever addresses it actually has.
Proxy these at your peril: an orange-clouded host record breaks SSH to that name.

**Service records** — from a service binding, pointed at the host that runs it:

```yaml
services:
  - name: gitea
    domains:
      - name: git.example.com
        proxied: true
```

Move the binding to another host and the record follows on the next run. That is
the whole reason DNS is generated rather than written.

Each domain's zone is determined by **longest suffix match** against the managed
zones, so `git.example.com` lands in `example.com` and a name under
`example.co.uk` is not mistaken for `co.uk`. A name matching no managed zone is
rejected.

## Rules enforced for you

- Only `A`, `AAAA` and `CNAME` may be proxied; the orange cloud is stripped from
  anything else, because Cloudflare cannot proxy it.
- A proxied record's TTL is forced to automatic, which is the only value
  Cloudflare accepts.
- Two hosts cannot claim the same domain — validation names both.
- A host with no known address produces no record, rather than a broken one.

## Applying

`dns` runs after `provision`, because a record cannot point at an address that
does not exist yet. Locally:

```bash
scripts/refresh_host_ips.sh                          # addresses from OpenTofu state
python3 scripts/render_tfvars.py --stack cloudflare
scripts/tofu.sh cloudflare default plan
```

Every apply is a **full** apply over the whole record set. Removing a domain
from a host file removes the record. That is intentional: the zone is a
projection of `hosts/`, not a place to keep things.

A record created by hand in the Cloudflare dashboard is invisible to this stack
and will survive — until its name collides with a managed record, at which point
the apply fails. Put it in `zones.yml` under `records:` instead.

## The token

`cloudflare/dns_api_token` needs `Zone:Read` and `DNS:Edit`, scoped to exactly
the zones in `zones.yml`. Traefik's ACME token
(`cloudflare/acme_dns_token`) is separate and lives on every edge host; keeping
them apart means a compromised host cannot rewrite the zone.

## Adding tunnels later

`ingress: traefik` and Cloudflare Tunnels are complementary: run `cloudflared`
as a stack, point its ingress rules at the local Traefik, and switch the
affected service records to `CNAME`. The zone scoping here already supports it —
see [alternatives.md](alternatives.md#5-what-terminates-tls).
