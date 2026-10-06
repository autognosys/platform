# Builds the Autognosys VM image: runtimes, Caddy, OTel Collector, OpenObserve,
# and an initial build of the app. No secrets are baked in; autognosys-boot.sh
# fetches them from GCP Secret Manager at boot.
#
# Usage (from this directory, with Application Default Credentials set up via
# `gcloud auth application-default login`):
#   packer init .
#   packer build .

packer {
  required_version = ">= 1.9.0"
  required_plugins {
    googlecompute = {
      version = ">= 1.1.4"
      source  = "github.com/hashicorp/googlecompute"
    }
    ansible = {
      version = ">= 1.1.1"
      source  = "github.com/hashicorp/ansible"
    }
  }
}

variable "project_id" {
  type    = string
  default = "autognosys-net"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "network" {
  type        = string
  default     = "autognosys-network"
  description = "Auto-mode VPC from infra/__main__.py; its firewall already allows SSH."
}

variable "machine_type" {
  type    = string
  default = "e2-medium"
}

locals {
  timestamp = formatdate("YYYYMMDD-hhmm", timestamp())
}

source "googlecompute" "base" {
  project_id              = var.project_id
  zone                    = var.zone
  network                 = var.network
  machine_type            = var.machine_type
  source_image_family     = "ubuntu-2404-lts-amd64"
  source_image_project_id = ["ubuntu-os-cloud"]
  disk_size               = 20
  disk_type               = "pd-balanced"
  ssh_username            = "packer"

  image_name        = "autognosys-base-${local.timestamp}"
  image_family      = "autognosys-base"
  image_description = "Autognosys VM image: Node, PM2, uv, Caddy, OTel Collector, OpenObserve, app build"
  image_labels = {
    built-by = "packer"
  }
}

build {
  sources = ["source.googlecompute.base"]

  provisioner "ansible" {
    playbook_file = "${path.root}/../ansible/site.yml"
    extra_arguments = [
      "-e", "bake=true",
      "-e", "ansible_python_interpreter=/usr/bin/python3",
    ]
    ansible_env_vars = ["ANSIBLE_HOST_KEY_CHECKING=False"]
  }

  # Persist the PM2 process list, stop the build-time daemon, and trim the image.
  provisioner "shell" {
    inline = [
      "sudo -H -u ubuntu pm2 save --force",
      "sudo -H -u ubuntu pm2 kill",
      "sudo apt-get clean",
      "sudo rm -rf /var/lib/apt/lists/*",
      "sudo journalctl --rotate && sudo journalctl --vacuum-time=1s || true",
    ]
  }
}
