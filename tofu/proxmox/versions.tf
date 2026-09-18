terraform {
  required_version = ">= 1.8.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.69"
    }
  }

  # One workspace per Proxmox node — `tofu workspace select pve01` — so a node
  # being down never blocks changes to another one.
  backend "http" {}
}

provider "proxmox" {
  endpoint  = var.endpoint.url
  api_token = var.proxmox_api_token
  insecure  = try(var.endpoint.insecure, false)

  # The provider shells into the node for snippet uploads and disk imports.
  ssh {
    agent    = false
    username = var.proxmox_ssh_username
    password = var.proxmox_ssh_password
  }
}
