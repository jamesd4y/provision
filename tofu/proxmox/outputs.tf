output "host_ips" {
  description = "Consumed by scripts/host_ips.py. Proxmox VMs are private, so only private_ipv4 is populated."
  value = {
    for name, vm in proxmox_virtual_environment_vm.host : name => {
      ipv4         = null
      ipv6         = null
      private_ipv4 = var.hosts[name].private_ipv4
    }
  }
}

output "vm_ids" {
  description = "Proxmox VM ids, so a host can be found in the web UI from a pipeline log."
  value       = { for name, vm in proxmox_virtual_environment_vm.host : name => vm.vm_id }
}

output "templates" {
  description = "Which template each host cloned."
  value       = local.template_names
}
