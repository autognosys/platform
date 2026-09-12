import pulumi
import pulumi_gcp as gcp

# ── Config ────────────────────────────────────────────────────────────────
config  = pulumi.Config("gcp")
project = config.require("project")
region  = config.get("region") or "us-central1"
zone    = f"{region}-a"
zone_b  = f"{region}-b"   # replica zone required for regional disk

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

# ── Persistent Data Disk (regional) ──────────────────────────────────────────
# Regional PDs are replicated across two zones and can be referenced in
# instance templates (zonal disks cannot). Replicated across -a and -b.
# Survives preemption and MIG recreation — OpenObserve data lives here.
data_disk = gcp.compute.RegionDisk(
    "autognosys-data",
    name          = "autognosys-data",
    region        = region,
    project       = project,
    type          = "pd-balanced",
    size          = 20,
    replica_zones = [zone, zone_b],
)

# ── Startup script ────────────────────────────────────────────────────────────
startup_script = """#!/bin/bash
set -e
LOG=/var/log/autognosys-startup.log
echo "[startup] $(date) begin" >> $LOG

# ── Base packages ─────────────────────────────────────────────────────────
apt-get update -y
apt-get install -y curl git vim htop unzip python3 python3-pip python3-venv

# ── Node.js 20 ────────────────────────────────────────────────────────────
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
npm install -g pm2

# ── Mount persistent data disk ────────────────────────────────────────────
DISK_DEVICE=/dev/disk/by-id/google-autognosys-data
MOUNT_POINT=/data

mkdir -p $MOUNT_POINT

# Only format if no filesystem exists (safe on re-attach after preemption)
if ! blkid $DISK_DEVICE > /dev/null 2>&1; then
    echo "[startup] Formatting data disk" >> $LOG
    mkfs.ext4 -F $DISK_DEVICE
fi

# Mount if not already mounted
if ! mountpoint -q $MOUNT_POINT; then
    mount $DISK_DEVICE $MOUNT_POINT
    echo "[startup] Mounted $DISK_DEVICE at $MOUNT_POINT" >> $LOG
fi

# Persist mount across reboots via fstab
DISK_UUID=$(blkid -s UUID -o value $DISK_DEVICE)
if ! grep -q "$DISK_UUID" /etc/fstab; then
    echo "UUID=$DISK_UUID $MOUNT_POINT ext4 discard,defaults 0 2" >> /etc/fstab
    echo "[startup] Added $MOUNT_POINT to fstab" >> $LOG
fi

# ── OpenObserve data dir on persistent disk ───────────────────────────────
mkdir -p $MOUNT_POINT/openobserve
chown -R root:root $MOUNT_POINT/openobserve

echo "[startup] $(date) complete" >> $LOG
"""

# ── Health Check ──────────────────────────────────────────────────────────────
health_check = gcp.compute.HealthCheck(
    "autognosys-hc",
    name    = "autognosys-hc",
    project = project,
    http_health_check = gcp.compute.HealthCheckHttpHealthCheckArgs(
        port         = 8000,
        request_path = "/api/health",
    ),
    check_interval_sec  = 30,
    timeout_sec         = 5,
    healthy_threshold   = 2,
    unhealthy_threshold = 3,
)

# ── Instance Template ─────────────────────────────────────────────────────────
instance_template = gcp.compute.InstanceTemplate(
    "autognosys-template",
    name_prefix  = "autognosys-template-",
    machine_type = "e2-medium",
    project      = project,
    region       = region,

    scheduling = gcp.compute.InstanceTemplateSchedulingArgs(
    preemptible             = True,
    provisioning_model      = "SPOT",
    instance_termination_action = "STOP",
    automatic_restart       = False,
    on_host_maintenance     = "TERMINATE",
    ),

    disks = [
        # Boot disk — ephemeral, recreated with each VM
        gcp.compute.InstanceTemplateDiskArgs(
            boot         = True,
            auto_delete  = True,
            source_image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64",
            disk_size_gb = 20,
            disk_type    = "pd-balanced",
        ),
        # Persistent data disk — regional, survives preemption
        gcp.compute.InstanceTemplateDiskArgs(
            boot        = False,
            auto_delete = False,
            source      = data_disk.self_link,
            device_name = "autognosys-data",
        ),
    ],

    network_interfaces = [
        gcp.compute.InstanceTemplateNetworkInterfaceArgs(
            network = network.self_link,
            access_configs = [
                gcp.compute.InstanceTemplateNetworkInterfaceAccessConfigArgs(
                    nat_ip = static_ip.address,
                ),
            ],
        ),
    ],

    metadata = {
        "ssh-keys": "ubuntu:ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCQm8TdbgXX4Unr7sndrWqAjzPM/cq6ad1J0mCNlke2QC3lPxqCOqne1FFNE9Lqu2Rg/b7itfBprCy4oohnRPHvPgzoTGGwvkXIDRF43hMAFuHMzDYP72dpoLyyRdkDL8uVWKTvejSuR0IC0ydYrLazDD+sFbG4piMIhCKdWOXiKxWQiBp6SbbIfWvFdg7zMafiK/4cuG1ed7Tp2uHip1IWqIe/KqBLj/h/E4wBwfKgvBGDMQG83Livc8L3u+y6uRDIXmm/7pNMyUm4GhbVgg63RRyqLGUhu2cJmLwV0fmRAU8ExzS3cWwO1j4a+yrmKQvNaJAtzdNdLjEdw5p8eb4mIA7Tj50whrmVEwOJV3NuuPiN2seOv38Ly8ofdioh6gHXAxXHDsLxA6kFgmQkzivv1cZ5XWFMU3sE6dfL/NcgOojfhv9r/iL1kCCg0AaN2f/fTby2ohA/4FuH1uGJHM4U1wID+fo81TnCVjdtJwPFT7nbZhAxatWlzS+XgnuMxxk= snyshadham@symphony",
        "startup-script": startup_script,
    },

    tags = ["autognosys", "http-server", "https-server"],

    opts = pulumi.ResourceOptions(depends_on=[firewall, data_disk]),
)

# ── Managed Instance Group ────────────────────────────────────────────────────
# ── Managed Instance Group ────────────────────────────────────────────────────
mig = gcp.compute.InstanceGroupManager(
    "autognosys-mig",
    name               = "autognosys-mig",
    zone               = zone,
    project            = project,
    base_instance_name = "autognosys",
    target_size        = 1,

    versions = [
        gcp.compute.InstanceGroupManagerVersionArgs(
            instance_template = instance_template.self_link_unique,
        ),
    ],

    stateful_disks = [
        gcp.compute.InstanceGroupManagerStatefulDiskArgs(
            device_name = "persistent-disk-0",
            delete_rule = "NEVER",
        ),
    ],

    auto_healing_policies = gcp.compute.InstanceGroupManagerAutoHealingPoliciesArgs(
        health_check      = health_check.self_link,
        initial_delay_sec = 300,
    ),
)
# ── Outputs ───────────────────────────────────────────────────────────────────
pulumi.export("external_ip", static_ip.address)
pulumi.export("zone",        zone)
pulumi.export("mig_name",    mig.name)
pulumi.export("data_disk",   data_disk.name)
pulumi.export("ssh_command", pulumi.Output.concat("ssh ubuntu@", static_ip.address))
