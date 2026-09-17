output "host_ips" {
  description = "Consumed by scripts/host_ips.py, which feeds the Cloudflare stack and the Ansible inventory."
  value = {
    for name, server in hcloud_server.host : name => {
      ipv4         = server.ipv4_address != "" ? server.ipv4_address : null
      ipv6         = server.ipv6_address != "" ? server.ipv6_address : null
      private_ipv4 = try(hcloud_server_network.private[name].ip, null)
    }
  }
}

output "server_types" {
  description = "What each host's cpu/memory actually resolved to, so a plan diff is readable."
  value       = local.server_type
}

output "volumes" {
  description = "Extra disks and the device path Ansible should format and mount."
  value = {
    for key, volume in hcloud_volume.extra : key => {
      host    = local.volumes[key].host
      device  = volume.linux_device
      mount   = try(local.volumes[key].disk.mount, null)
      format  = local.volumes[key].disk.format
      size_gb = volume.size
    }
  }
}
