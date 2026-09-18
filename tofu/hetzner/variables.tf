# Everything except the token comes from tofu/hetzner/vars/<folder>.tfvars.json,
# which scripts/render_tfvars.py generates from hosts/<folder>/.

variable "hcloud_token" {
  description = "Hetzner Cloud API token, injected from Bitwarden Secrets Manager by CI."
  type        = string
  sensitive   = true
}

variable "provider_name" {
  description = "The hosts/ folder this run is for. Also the OpenTofu workspace name."
  type        = string
}

variable "hosts" {
  description = "hostname -> host definition, rendered from hosts/<folder>/<hostname>.yml."
  type = map(object({
    hostname     = string
    fqdn         = string
    description  = optional(string, "")
    cpu          = number
    memory_mb    = number
    architecture = optional(string, "x86")
    root_disk_gb = number
    extra_disks = optional(list(object({
      name    = string
      size_gb = number
      type    = optional(string, "ssd")
      mount   = optional(string)
      format  = optional(string, "ext4")
    })), [])
    image        = optional(string, "")
    distribution = string
    os_version   = string
    timezone     = optional(string, "UTC")
    ipv4_enabled = optional(bool, true)
    ipv6_enabled = optional(bool, true)
    private_ipv4 = optional(string)
    firewall_rules = optional(list(object({
      name     = string
      port     = string
      protocol = optional(string, "tcp")
      source   = optional(list(string), ["0.0.0.0/0", "::/0"])
    })), [])
    ssh_user = optional(string, "deploy")
    labels   = optional(map(string), {})
    options  = optional(any, {})
  }))
  default = {}
}

variable "ssh_keys" {
  description = "Public keys installed on every host in this folder."
  type = list(object({
    name       = string
    public_key = string
  }))
  default = []
}

variable "network" {
  description = "Private network for this folder, if any."
  type        = any
  default     = {}
}

variable "defaults" {
  description = "Folder-level defaults (location, ...)."
  type        = any
  default     = {}
}

variable "credentials" {
  description = "Bitwarden secret KEYS, carried through for documentation only."
  type        = map(string)
  default     = {}
}

variable "endpoint" {
  description = "Unused for Hetzner; present so one renderer serves every stack."
  type        = any
  default     = {}
}

variable "tailscale_auth_key" {
  description = "Short-lived, tagged, pre-authorised Tailscale auth key, minted per apply by scripts/tailscale_authkey.sh. Empty disables first-boot registration."
  type        = string
  sensitive   = true
  default     = ""
}

variable "tailscale_tags" {
  description = "Tags applied to hosts at first boot. Must be tags the OAuth client owns."
  type        = string
  default     = "tag:server"
}

variable "_banner" {
  description = "Provenance note written by the renderer."
  type        = string
  default     = ""
}
