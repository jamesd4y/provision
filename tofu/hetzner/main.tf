data "hcloud_server_types" "all" {}

locals {
  # cpu + memory in the host YAML are the source of truth; this picks the
  # smallest non-deprecated server type that satisfies both. Pin one explicitly
  # with provider_options.server_type when the automatic choice is wrong.
  architecture = { x86 = "x86", arm = "arm" }

  candidates = {
    for name, host in var.hosts : name => {
      for type in data.hcloud_server_types.all.server_types :
      format("%04d-%07d-%05d-%s", type.cores, floor(type.memory * 1024), type.disk, type.name) => type.name
      if type.cores >= host.cpu
      && (type.memory * 1024) >= host.memory_mb
      && type.architecture == lookup(local.architecture, host.architecture, "x86")
      && !type.is_deprecated
    }
  }

  server_type = {
    for name, host in var.hosts : name => coalesce(
      try(host.options.server_type, null),
      try(local.candidates[name][sort(keys(local.candidates[name]))[0]], null),
      "NO-MATCHING-SERVER-TYPE"
    )
  }

  image = {
    for name, host in var.hosts : name => (
      host.image != "" ? host.image : "${host.distribution}-${host.os_version}"
    )
  }

  location = {
    for name, host in var.hosts : name => coalesce(
      try(host.options.location, null),
      try(var.defaults.location, null),
      "nbg1"
    )
  }

  private_network = try(var.network.private_network, null)

  # Only hosts that asked for a private address get attached to the network.
  networked_hosts = {
    for name, host in var.hosts : name => host
    if local.private_network != null && host.private_ipv4 != null
  }

  volumes = merge([
    for name, host in var.hosts : {
      for disk in host.extra_disks :
      "${name}/${disk.name}" => {
        host     = name
        disk     = disk
        location = local.location[name]
      }
    }
  ]...)
}

resource "hcloud_ssh_key" "fleet" {
  for_each = { for key in var.ssh_keys : key.name => key }

  name       = "${var.provider_name}-${each.value.name}"
  public_key = each.value.public_key
  labels     = { managed_by = "provision" }
}

resource "hcloud_network" "private" {
  count = local.private_network == null ? 0 : 1

  name     = local.private_network.name
  ip_range = local.private_network.cidr
  labels   = { managed_by = "provision" }
}

resource "hcloud_network_subnet" "private" {
  count = local.private_network == null ? 0 : 1

  network_id   = hcloud_network.private[0].id
  type         = "cloud"
  network_zone = try(local.private_network.zone, "eu-central")
  ip_range     = local.private_network.cidr
}

resource "hcloud_firewall" "host" {
  for_each = { for name, host in var.hosts : name => host if length(host.firewall_rules) > 0 }

  name   = "${each.key}-fw"
  labels = { managed_by = "provision", host = each.key }

  dynamic "rule" {
    for_each = each.value.firewall_rules
    content {
      direction   = "in"
      protocol    = rule.value.protocol
      port        = rule.value.port
      source_ips  = rule.value.source
      description = rule.value.name
    }
  }

  # Outbound ICMP so the host can be diagnosed from inside.
  rule {
    direction       = "out"
    protocol        = "icmp"
    destination_ips = ["0.0.0.0/0", "::/0"]
    description     = "icmp out"
  }
}

resource "hcloud_volume" "extra" {
  for_each = local.volumes

  name              = replace(each.key, "/", "-")
  size              = each.value.disk.size_gb
  location          = each.value.location
  format            = each.value.disk.format == "none" ? null : each.value.disk.format
  delete_protection = true
  labels            = { managed_by = "provision", host = each.value.host }
}

resource "hcloud_server" "host" {
  for_each = var.hosts

  name         = each.key
  server_type  = local.server_type[each.key]
  image        = local.image[each.key]
  location     = local.location[each.key]
  ssh_keys     = [for key in hcloud_ssh_key.fleet : key.id]
  firewall_ids = try([hcloud_firewall.host[each.key].id], [])
  labels = merge(each.value.labels, {
    managed_by = "provision"
    provider   = var.provider_name
  })

  public_net {
    ipv4_enabled = each.value.ipv4_enabled
    ipv6_enabled = each.value.ipv6_enabled
  }

  user_data = templatefile("${path.module}/../templates/cloud-init.yaml.tftpl", {
    hostname           = each.value.hostname
    fqdn               = each.value.fqdn
    timezone           = each.value.timezone
    ssh_user           = each.value.ssh_user
    provider_name      = var.provider_name
    ssh_keys           = [for key in var.ssh_keys : key.public_key]
    tailscale_auth_key = var.tailscale_auth_key
    tailscale_hostname = each.value.hostname
    tailscale_tags     = var.tailscale_tags
  })

  lifecycle {
    # cloud-init only ever runs at first boot, so its content is irrelevant to a
    # host that already exists — but hcloud marks user_data and ssh_keys as
    # ForceNew. Without this, a per-apply Tailscale bootstrap key (or an added
    # admin key) would silently destroy and recreate the whole fleet. Ansible
    # owns both of these from here on.
    ignore_changes = [user_data, ssh_keys]

    precondition {
      condition     = local.server_type[each.key] != "NO-MATCHING-SERVER-TYPE"
      error_message = "No Hetzner server type has >= ${each.value.cpu} vCPU and >= ${each.value.memory_mb} MiB (${each.value.architecture}). Lower hardware.cpu/memory or set provider_options.server_type in hosts/${var.provider_name}/${each.key}.yml."
    }
    precondition {
      # Hetzner disks come with the server type; asking for more root than it has
      # is a silent no-op otherwise, and the host runs out of space later.
      condition     = each.value.root_disk_gb <= try([for t in data.hcloud_server_types.all.server_types : t.disk if t.name == local.server_type[each.key]][0], 0)
      error_message = "Server type ${local.server_type[each.key]} for ${each.key} has a smaller root disk than hardware.storage requests. Add an extra disk (a Hetzner volume) instead, or pick a larger server type."
    }
    # Rebuilding a host destroys its data. Remove the file from hosts/ and apply
    # deliberately when you really mean to.
    prevent_destroy = true
  }
}

resource "hcloud_server_network" "private" {
  for_each = local.networked_hosts

  server_id  = hcloud_server.host[each.key].id
  network_id = hcloud_network.private[0].id
  ip         = each.value.private_ipv4

  depends_on = [hcloud_network_subnet.private]
}

resource "hcloud_volume_attachment" "extra" {
  for_each = local.volumes

  volume_id = hcloud_volume.extra[each.key].id
  server_id = hcloud_server.host[each.value.host].id
  automount = false
}
