terraform {
  required_version = ">= 1.8.0"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.51"
    }
  }

  # State lives outside the repo. The HTTP backend works with Woodpecker's own
  # host or any Terraform-compatible state server; swap in s3/R2 if you prefer.
  # Configured with `tofu init -backend-config=...` so no credentials land here.
  backend "http" {}
}

provider "hcloud" {
  token = var.hcloud_token
}
