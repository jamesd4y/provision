# generated.auto.tfvars.json is written by scripts/render_tfvars.py from
# cloudflare/zones.yml plus every host's dns block and service domains.

variable "cloudflare_api_token" {
  description = "Token with Zone:Read + DNS:Edit on the managed zones, injected from Bitwarden Secrets Manager by CI."
  type        = string
  sensitive   = true
}

variable "zones" {
  description = "Zone name -> { zone_id }. Only these zones may be written to."
  type = map(object({
    zone_id = string
  }))
  default = {}
}

variable "records" {
  description = "Stable key -> record. The key encodes what produced the record, so renaming a host moves one record instead of recreating the zone."
  type = map(object({
    zone     = string
    name     = string
    type     = string
    value    = string
    ttl      = optional(number, 1)
    priority = optional(number)
    proxied  = optional(bool, false)
    comment  = optional(string, "")
  }))
  default = {}
}

variable "account_id_secret" {
  description = "Bitwarden key for the account id. Carried through for documentation."
  type        = string
  default     = ""
}

variable "api_token_secret" {
  description = "Bitwarden key for the API token. Carried through for documentation."
  type        = string
  default     = ""
}

variable "_banner" {
  description = "Provenance note written by the renderer."
  type        = string
  default     = ""
}
