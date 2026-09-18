locals {
  node              = var.endpoint.node
  bridge            = try(var.network.bridge, "vmbr0")
  vlan_id           = try(var.network.vlan_id, null)
  gateway           = try(var.network.gateway, null)
  prefix            = try(var.network.prefix, 24)
  nameservers       = try(var.network.nameservers, [])
  search_domain     = try(var.network.search_domain, null)
  snippet_datastore = try(var.network.snippet_datastore, "local")

  # Every host clones a cloud-init template. The template name comes from the
  # host, then the folder default.
  template_names = {
    for name, host in var.hosts : name => coalesce(
      try(host.options.template, null),
      try(var.defaults.template, null),
      "${host.distribution}-${host.os_version}-cloudinit"
    )
  }

  datastore = {
    for name, host in var.hosts : name => coalesce(
      try(host.options.datastore, null),
      try(var.defaults.datastore, null),
      "local-lvm"
    )
  }

  # scsi0 is the cloned root disk; extra disks start at scsi1 in YAML order.
  extra_disks = merge([
    for name, host in var.hosts : {
      for index, disk in host.extra_disks :
      "${name}/${disk.name}" => {
        host      = name
        disk      = disk
        interface = "scsi${index + 1}"
      }
    }
  ]...)
}

data "proxmox_virtual_environment_vms" "templates" {
  node_name = local.node

  filter {
    name   = "template"
    values = ["true"]
  }
}

locals {
  template_ids = {
    for name, template in local.template_names : name => try(
      [for vm in data.proxmox_virtual_environment_vms.templates.vms : vm.vm_id if vm.name == template][0],
      null
    )
  }
}

resource "proxmox_virtual_environment_file" "cloud_init" {
  for_each = var.hosts

  node_name    = local.node
  datastore_id = local.snippet_datastore
  content_type = "snippets"

  source_raw {
    # Ignition reads its config from the cloud-init drive's user-data on
    # Proxmox (platform id `proxmoxve`), so a MicroOS host gets JSON here where
    # a Debian host gets cloud-config.
    file_name = each.value.ignition != "" ? "${each.key}-config.ign" : "${each.key}-user-data.yaml"
    data = each.value.ignition != "" ? replace(
      each.value.ignition, "__TS_AUTH_KEY__", var.tailscale_auth_key
      ) : templatefile("${path.module}/../templates/cloud-init.yaml.tftpl", {
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
  }

  lifecycle {
    # The snippet is read once, at first boot. Re-rendering it with a fresh
    # bootstrap key must not churn the file or disturb the VM that points at it.
    ignore_changes = [source_raw]
  }
}

resource "proxmox_virtual_environment_vm" "host" {
  for_each = var.hosts

  name        = each.key
  description = each.value.description
  node_name   = local.node
  vm_id       = try(each.value.options.vm_id, null)
  tags        = sort(concat(keys(each.value.labels), ["provision"]))
  on_boot     = try(each.value.options.start_on_boot, true)

  clone {
    vm_id = local.template_ids[each.key]
    full  = true
  }

  agent {
    enabled = true
  }

  cpu {
    cores = each.value.cpu
    type  = "host"
  }

  memory {
    dedicated = each.value.memory_mb
  }

  # The cloned root disk, grown to the size the host asks for. Proxmox can grow
  # a disk in place but never shrink one, so reducing hardware.storage[0].size
  # is rejected by the precondition below rather than silently ignored.
  disk {
    datastore_id = local.datastore[each.key]
    interface    = "scsi0"
    size         = each.value.root_disk_gb
    file_format  = "raw"
    ssd          = true
    discard      = "on"
  }

  dynamic "disk" {
    for_each = { for key, extra in local.extra_disks : key => extra if extra.host == each.key }
    content {
      datastore_id = local.datastore[each.key]
      interface    = disk.value.interface
      size         = disk.value.disk.size_gb
      file_format  = "raw"
      ssd          = disk.value.disk.type != "hdd"
      discard      = "on"
    }
  }

  network_device {
    bridge  = local.bridge
    vlan_id = local.vlan_id
    model   = "virtio"
  }

  initialization {
    datastore_id      = local.datastore[each.key]
    user_data_file_id = proxmox_virtual_environment_file.cloud_init[each.key].id

    dynamic "ip_config" {
      for_each = each.value.private_ipv4 == null ? [] : [1]
      content {
        ipv4 {
          address = "${each.value.private_ipv4}/${local.prefix}"
          gateway = local.gateway
        }
      }
    }

    dynamic "ip_config" {
      for_each = each.value.private_ipv4 == null ? [1] : []
      content {
        ipv4 {
          address = "dhcp"
        }
      }
    }

    dynamic "dns" {
      for_each = length(local.nameservers) > 0 ? [1] : []
      content {
        servers = local.nameservers
        domain  = local.search_domain
      }
    }
  }

  lifecycle {
    precondition {
      condition     = local.template_ids[each.key] != null
      error_message = "No template named '${local.template_names[each.key]}' on node ${local.node}. Build the cloud-init template first (see docs/provisioning.md) or set provider_options.template."
    }
    precondition {
      condition     = each.value.private_ipv4 == null || local.gateway != null
      error_message = "${each.key} has a static address but hosts/${var.provider_name}/_config.yml sets no network.gateway."
    }
    prevent_destroy = true
  }
}
