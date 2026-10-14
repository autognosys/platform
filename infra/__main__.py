import pulumi
import pulumi_gcp as gcp

from auto_remediation import create_auto_remediation
from boot_secrets import create_boot_secrets
from load_balancer import NAMED_PORTS, create_load_balancer

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
# SSH only. Web traffic reaches the VM from the load balancer's Google source
# ranges (see load_balancer.py); ports 80/443 were Caddy's and are closed.
firewall = gcp.compute.Firewall(
    "autognosys-firewall",
    name    = "autognosys-firewall",
    network = network.self_link,
    project = project,
    allows  = [
        gcp.compute.FirewallAllowArgs(protocol="tcp", ports=["22"]),
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

# ── Static External IP (no longer attached) ───────────────────────────────────
# The instance template no longer pins this address: the load balancer owns the
# public IP (autognosys-lb-ip), and a pinned address would stop the MIG from
# surging a second instance. Kept only so this change does not release the
# address while the old VM may still hold it. Delete in a follow-up PR once the
# rollout has finished and `gcloud compute addresses describe autognosys-ip`
# shows no users.
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
            # Empty access config = ephemeral external IP, needed for outbound
            # traffic (apt, git pull, Secret Manager). Inbound web traffic
            # arrives through the load balancer, and the firewall allows SSH only.
            access_configs = [
                gcp.compute.InstanceTemplateNetworkInterfaceAccessConfigArgs(),
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

    # Named ports the load balancer's backend services route to (load_balancer.py).
    named_ports = [
        gcp.compute.InstanceGroupManagerNamedPortArgs(name=n, port=p)
        for n, p in NAMED_PORTS.items()
    ],

    versions = [
        gcp.compute.InstanceGroupManagerVersionArgs(
            instance_template = instance_template.self_link_unique,
        ),
    ],

    auto_healing_policies = gcp.compute.InstanceGroupManagerAutoHealingPoliciesArgs(
        health_check      = health_check.self_link,
        initial_delay_sec = 300,
    ),

    # SUBSTITUTE with surge: a new instance (fresh name, fresh boot disk) is
    # created and must pass the autohealing health check before the old one is
    # deleted, and max_unavailable 0 keeps the old one serving meanwhile. The
    # load balancer owns the public IP, so a rollout no longer means downtime.
    # Needs room for two e2-medium spot instances during a rollout.
    update_policy = gcp.compute.InstanceGroupManagerUpdatePolicyArgs(
        type                  = "PROACTIVE",
        minimal_action        = "REPLACE",
        replacement_method    = "SUBSTITUTE",
        max_surge_fixed       = 1,
        max_unavailable_fixed = 0,
    ),
)
# ── Global load balancer + Google-managed certificate ─────────────────────────
lb_ip, lb_dns_auth, _, _, _ = create_load_balancer(
    project=project, network=network, mig=mig, api_health_check=health_check,
)

# ── Preemption auto-remediation Cloud Function ────────────────────────────────
auto_remediation_function = create_auto_remediation(project=project, region=region)

# ── Outputs ───────────────────────────────────────────────────────────────────
pulumi.export("lb_ip",       lb_ip.address)   # switch the autognosys.net A record here LAST
# CNAME to add at the DNS host so the certificate can validate (step 1 of cutover):
pulumi.export("dns_auth_record_name", lb_dns_auth.dns_resource_records[0].name)
pulumi.export("dns_auth_record_type", lb_dns_auth.dns_resource_records[0].type)
pulumi.export("dns_auth_record_data", lb_dns_auth.dns_resource_records[0].data)
pulumi.export("zone",        zone)
pulumi.export("mig_name",    mig.name)
pulumi.export("data_disk",   data_disk.name)
# Instance names and (ephemeral) IPs change on every recreation, so SSH by name:
pulumi.export("ssh_command", pulumi.Output.concat(
    "gcloud compute instance-groups managed list-instances ", mig.name,
    " --zone ", zone, "  # then: gcloud compute ssh ubuntu@<instance> --zone ", zone))
pulumi.export("auto_remediation_function_name", auto_remediation_function.name)
pulumi.export("vm_service_account", vm_service_account.email)
