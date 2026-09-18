locals {
  # Cloudflare only proxies HTTP-ish traffic, so an orange cloud on anything
  # else is a configuration error that silently breaks the record.
  proxyable_types = ["A", "AAAA", "CNAME"]

  records = {
    for key, record in var.records : key => merge(record, {
      zone_id = var.zones[record.zone].zone_id
      proxied = contains(local.proxyable_types, record.type) ? record.proxied : false
      # TTL must be 1 ("automatic") whenever a record is proxied.
      ttl = contains(local.proxyable_types, record.type) && record.proxied ? 1 : record.ttl
    })
  }
}

resource "cloudflare_dns_record" "managed" {
  for_each = local.records

  zone_id  = each.value.zone_id
  name     = each.value.name
  type     = each.value.type
  content  = each.value.value
  ttl      = each.value.ttl
  proxied  = each.value.proxied
  priority = each.value.priority
  comment  = each.value.comment

  lifecycle {
    precondition {
      condition     = contains(keys(var.zones), each.value.zone)
      error_message = "Record ${each.key} targets zone ${each.value.zone}, which is not in cloudflare/zones.yml."
    }
  }
}
