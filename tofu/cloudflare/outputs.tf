output "records" {
  description = "What this stack owns, so a drift check can diff it against the zone."
  value = {
    for key, record in cloudflare_dns_record.managed : key => {
      fqdn    = record.name
      type    = record.type
      content = record.content
      proxied = record.proxied
    }
  }
}

output "record_count" {
  value = length(cloudflare_dns_record.managed)
}
