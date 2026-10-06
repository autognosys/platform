import pulumi
import pulumi_gcp as gcp

from auto_remediation import create_auto_remediation
from boot_secrets import create_boot_secrets

# ── Config ────────────────────────────────────────────────────────────────
config  = pulumi.Config("gcp")
project = config.require("project")
region  = config.get("region") or "us-central1"
zone    = f"{region}-a"
zone_b  = f"{region}-b"   # replica zone of the (now unattached) regional disk

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

# ── Firewall: Google health-check probes ──────────────────────────────────────
# The MIG autohealing check (and later the load balancer's) probes port 8000
# from Google's fixed probe ranges. Without this rule every probe times out and
# the MIG reports the instance as unhealthy even though the app is fine.
# Scoped to the instance tag instead of the whole network.
health_check_firewall = gcp.compute.Firewall(
    "autognosys-allow-health-checks",
    name          = "autognosys-allow-health-checks",
    network       = network.self_link,
    project       = project,
    allows        = [
        gcp.compute.FirewallAllowArgs(protocol="tcp", ports=["8000"]),
    ],
    source_ranges = ["130.211.0.0/22", "35.191.0.0/16"],
    target_tags   = ["autognosys"],
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
# NO LONGER ATTACHED to anything after the image rollout (OpenObserve data now
# lives on the boot disk). Kept only so Pulumi does not try to delete it while the
# old VM may still hold it. Remove in a follow-up PR once the old VM is gone.
data_disk = gcp.compute.RegionDisk(
    "autognosys-data",
    name          = "autognosys-data",
    region        = region,
    project       = project,
    type          = "pd-balanced",
    size          = 20,
    replica_zones = [zone, zone_b],
)

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

# ── Boot-time secrets (Secret Manager) + dedicated VM service account ─────────
vm_service_account, boot_secrets = create_boot_secrets(project=project)

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

    # Single ephemeral boot disk, created from the Packer-built image
    # (packer/autognosys-base.pkr.hcl). The image family always resolves to the
    # newest build; no data disk — OpenObserve data lives on the boot disk and
    # is disposable for now.
    disks = [
        gcp.compute.InstanceTemplateDiskArgs(
            boot         = True,
            auto_delete  = True,
            source_image = f"projects/{project}/global/images/family/autognosys-base",
            disk_size_gb = 20,
            disk_type    = "pd-balanced",
        ),
    ],

    # Dedicated identity that can read only the two boot-time secrets
    # (see boot_secrets.py). cloud-platform scope; IAM does the restricting.
    service_account = gcp.compute.InstanceTemplateServiceAccountArgs(
        email  = vm_service_account.email,
        scopes = ["https://www.googleapis.com/auth/cloud-platform"],
    ),

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
    },

    tags = ["autognosys", "http-server", "https-server"],

    opts = pulumi.ResourceOptions(depends_on=[firewall]),
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

    auto_healing_policies = gcp.compute.InstanceGroupManagerAutoHealingPoliciesArgs(
        health_check      = health_check.self_link,
        initial_delay_sec = 300,
    ),

    # SUBSTITUTE deletes the old instance first (max_surge 0 — the template pins
    # the static IP, so a surge instance could not get it) and then creates a
    # new one with a fresh name and a fresh boot disk. Expect a few minutes of
    # downtime per rollout until the load balancer owns the public IP.
    update_policy = gcp.compute.InstanceGroupManagerUpdatePolicyArgs(
        type                  = "PROACTIVE",
        minimal_action        = "REPLACE",
        replacement_method    = "SUBSTITUTE",
        max_surge_fixed       = 0,
        max_unavailable_fixed = 1,
    ),
)
# ── Preemption auto-remediation Cloud Function ────────────────────────────────
auto_remediation_function = create_auto_remediation(project=project, region=region)

# ── Outputs ───────────────────────────────────────────────────────────────────
pulumi.export("external_ip", static_ip.address)
pulumi.export("zone",        zone)
pulumi.export("mig_name",    mig.name)
pulumi.export("data_disk",   data_disk.name)
pulumi.export("ssh_command", pulumi.Output.concat("ssh ubuntu@", static_ip.address))
pulumi.export("auto_remediation_function_name", auto_remediation_function.name)
pulumi.export("vm_service_account", vm_service_account.email)
