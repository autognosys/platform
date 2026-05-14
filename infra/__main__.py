import pulumi
import pulumi_gcp as gcp

# ── Config ────────────────────────────────────────────────────────────────
config  = pulumi.Config("gcp")
project = config.require("project")
region  = config.get("region") or "us-central1"
zone    = f"{region}-a"

# ── VPC Network ───────────────────────────────────────────────────────────────
network = gcp.compute.Network(
    "autognosys-network",
    name                    = "autognosys-network",
    auto_create_subnetworks = True,
    project                 = project,
)

# ── Firewall ──────────────────────────────────────────────────────────────────
firewall = gcp.compute.Firewall(
    "autognosys-firewall",
    name    = "autognosys-firewall",
    network = network.self_link,
    project = project,
    allows  = [
        gcp.compute.FirewallAllowArgs(protocol="tcp", ports=["22"]),
        gcp.compute.FirewallAllowArgs(protocol="tcp", ports=["80"]),
        gcp.compute.FirewallAllowArgs(protocol="tcp", ports=["443"]),
    ],
    source_ranges = ["0.0.0.0/0"],
)

# ── Static External IP ────────────────────────────────────────────────────────
static_ip = gcp.compute.Address(
    "autognosys-ip",
    name    = "autognosys-ip",
    region  = region,
    project = project,
)

# ── Startup script ────────────────────────────────────────────────────────────
startup_script = """#!/bin/bash
set -e
apt-get update -y
apt-get install -y curl git vim htop unzip

# Python 3
apt-get install -y python3 python3-pip python3-venv

# Node.js 20
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs

# PM2
npm install -g pm2

echo "Autognosys VM bootstrap complete" >> /var/log/autognosys-startup.log
"""

# ── VM ────────────────────────────────────────────────────────────────────────
vm = gcp.compute.Instance(
    "autognosys-vm",
    name         = "autognosys-vm",
    machine_type = "e2-medium",
    zone         = zone,
    project      = project,

    scheduling = gcp.compute.InstanceSchedulingArgs(
        preemptible         = True,
        automatic_restart   = False,
        on_host_maintenance = "TERMINATE",
    ),

    boot_disk = gcp.compute.InstanceBootDiskArgs(
        auto_delete = False,                  # survive VM deletion
        initialize_params = gcp.compute.InstanceBootDiskInitializeParamsArgs(
            image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64",
            size  = 50,
            type  = "pd-balanced",
        ),
    ),

    network_interfaces = [
        gcp.compute.InstanceNetworkInterfaceArgs(
            network = network.self_link,
            access_configs = [
                gcp.compute.InstanceNetworkInterfaceAccessConfigArgs(
                    nat_ip = static_ip.address,
                ),
            ],
        ),
    ],
    metadata = {
        "ssh-keys": "ubuntu:ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCQm8TdbgXX4Unr7sndrWqAjzPM/cq6ad1J0mCNlke2QC3lPxqCOqne1FFNE9Lqu2Rg/b7itfBprCy4oohnRPHvPgzoTGGwvkXIDRF43hMAFuHMzDYP72dpoLyyRdkDL8uVWKTvejSuR0IC0ydYrLazDD+sFbG4piMIhCKdWOXiKxWQiBp6SbbIfWvFdg7zMafiK/4cuG1ed7Tp2uHip1IWqIe/KqBLj/h/E4wBwfKgvBGDMQG83Livc8L3u+y6uRDIXmm/7pNMyUm4GhbVgg63RRyqLGUhu2cJmLwV0fmRAU8ExzS3cWwO1j4a+yrmKQvNaJAtzdNdLjEdw5p8eb4mIA7Tj50whrmVEwOJV3NuuPiN2seOv38Ly8ofdioh6gHXAxXHDsLxA6kFgmQkzivv1cZ5XWFMU3sE6dfL/NcgOojfhv9r/iL1kCCg0AaN2f/fTby2ohA/4FuH1uGJHM4U1wID+fo81TnCVjdtJwPFT7nbZhAxatWlzS+XgnuMxxk= snyshadham@symphony",
    },
    metadata_startup_script = startup_script,
    tags    = ["autognosys", "http-server", "https-server"],
    opts    = pulumi.ResourceOptions(depends_on=[firewall]),
)

# ── Outputs ───────────────────────────────────────────────────────────────────
pulumi.export("vm_name",     vm.name)
pulumi.export("external_ip", static_ip.address)
pulumi.export("zone",        vm.zone)
pulumi.export("ssh_command", pulumi.Output.concat(
    "ssh ubuntu@", static_ip.address
))  