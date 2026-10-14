"""Global external Application Load Balancer in front of the MIG.

Replaces what Caddy did on the VM:
  - TLS termination      -> Certificate Manager cert, DNS-authorized (no cert state on the VM)
  - path routing         -> URL map: /api/* -> :8000, /observability/* -> :5080, else :3000
  - HTTP -> HTTPS        -> separate redirect URL map on port 80

`instance_groups` is a list of instance-group URLs (zonal or regional MIGs); every
backend service load-balances across all of them.

The load balancer talks plain HTTP straight to the app ports on the instance, so
Caddy has been retired: it is gone from the image, and request logging on the
backend services replaces its access.log.

Cutover order (no outage):
  1. pulumi up, then add the exported CNAME (dns_auth_record_*) at the DNS host
  2. wait for the certificate to become ACTIVE
  3. verify the load balancer with curl --resolve, then switch the A record to lb_ip
"""
import pulumi
import pulumi_gcp as gcp

# Google's fixed probe / proxy source ranges (health checks and LB-to-backend traffic).
GOOGLE_LB_RANGES = ["130.211.0.0/22", "35.191.0.0/16"]

# Named ports the backend services refer to (declared on the MIG in __main__.py).
NAMED_PORTS = {"web": 3000, "api": 8000, "obs": 5080}


def create_load_balancer(
    project: str,
    network: gcp.compute.Network,
    instance_groups: list,
    api_health_check: gcp.compute.HealthCheck,
    domain: str = "autognosys.net",
):
    # ── Firewall: probes + LB traffic to all three app ports ──────────────────
    # The existing rule only opens 8000; the new backends need 3000 and 5080.
    lb_firewall = gcp.compute.Firewall(
        "autognosys-allow-lb-backends",
        name          = "autognosys-allow-lb-backends",
        network       = network.self_link,
        project       = project,
        allows        = [
            gcp.compute.FirewallAllowArgs(
                protocol="tcp", ports=[str(p) for p in NAMED_PORTS.values()],
            ),
        ],
        source_ranges = GOOGLE_LB_RANGES,
        target_tags   = ["autognosys"],
    )

    # ── Health checks for the two new backends (api reuses the existing one) ──
    web_hc = gcp.compute.HealthCheck(
        "autognosys-hc-web",
        name    = "autognosys-hc-web",
        project = project,
        http_health_check = gcp.compute.HealthCheckHttpHealthCheckArgs(
            port=NAMED_PORTS["web"], request_path="/",
        ),
        check_interval_sec  = 15,
        timeout_sec         = 5,
        healthy_threshold   = 2,
        unhealthy_threshold = 3,
    )
    obs_hc = gcp.compute.HealthCheck(
        "autognosys-hc-obs",
        name    = "autognosys-hc-obs",
        project = project,
        http_health_check = gcp.compute.HealthCheckHttpHealthCheckArgs(
            port=NAMED_PORTS["obs"], request_path="/healthz",
        ),
        check_interval_sec  = 15,
        timeout_sec         = 5,
        healthy_threshold   = 2,
        unhealthy_threshold = 3,
    )

    # ── Backend services (one per app port) ───────────────────────────────────
    def backend(name: str, port_name: str, hc: gcp.compute.HealthCheck):
        return gcp.compute.BackendService(
            f"autognosys-be-{name}",
            name                  = f"autognosys-be-{name}",
            project               = project,
            protocol              = "HTTP",
            port_name             = port_name,
            load_balancing_scheme = "EXTERNAL_MANAGED",
            timeout_sec           = 30,
            connection_draining_timeout_sec = 10,
            health_checks         = hc.id,
            log_config            = gcp.compute.BackendServiceLogConfigArgs(
                enable=True, sample_rate=1.0,
            ),
            backends              = [
                gcp.compute.BackendServiceBackendArgs(
                    group           = group,
                    balancing_mode  = "UTILIZATION",
                    capacity_scaler = 1.0,
                )
                for group in instance_groups
            ],
        )

    be_web = backend("web", "web", web_hc)
    be_api = backend("api", "api", api_health_check)
    be_obs = backend("obs", "obs", obs_hc)

    # ── URL map: mirrors the Caddyfile routing ────────────────────────────────
    url_map = gcp.compute.URLMap(
        "autognosys-urlmap",
        name            = "autognosys-urlmap",
        project         = project,
        default_service = be_web.id,
        host_rules      = [
            gcp.compute.URLMapHostRuleArgs(hosts=["*"], path_matcher="main"),
        ],
        path_matchers   = [
            gcp.compute.URLMapPathMatcherArgs(
                name            = "main",
                default_service = be_web.id,
                path_rules      = [
                    gcp.compute.URLMapPathMatcherPathRuleArgs(
                        paths=["/api/*"], service=be_api.id),
                    gcp.compute.URLMapPathMatcherPathRuleArgs(
                        paths=["/observability/*"], service=be_obs.id),
                ],
            ),
        ],
    )

    # ── Global static IP, managed certificate, HTTPS front end ────────────────
    lb_ip = gcp.compute.GlobalAddress(
        "autognosys-lb-ip",
        name    = "autognosys-lb-ip",
        project = project,
    )

    # Certificate Manager with DNS authorization: the cert is validated through a
    # CNAME at the DNS host (exported below), NOT by the domain pointing at this
    # load balancer. So it goes ACTIVE while the site is still served from the
    # old IP, and the A-record switch afterwards causes no TLS gap.
    # Needs the certificatemanager.googleapis.com API enabled on the project.
    dns_auth = gcp.certificatemanager.DnsAuthorization(
        "autognosys-dns-auth",
        name    = f"autognosys-dnsauth-{domain.replace('.', '-')}",
        project = project,
        domain  = domain,
    )
    cert = gcp.certificatemanager.Certificate(
        "autognosys-cert",
        name    = f"autognosys-cert-{domain.replace('.', '-')}",
        project = project,
        managed = gcp.certificatemanager.CertificateManagedArgs(
            domains            = [domain],
            dns_authorizations = [dns_auth.id],
        ),
    )
    cert_map = gcp.certificatemanager.CertificateMap(
        "autognosys-cert-map",
        name    = "autognosys-cert-map",
        project = project,
    )
    cert_map_entry = gcp.certificatemanager.CertificateMapEntry(
        "autognosys-cert-map-entry",
        name         = "autognosys-cert-map-entry",
        project      = project,
        map          = cert_map.name,
        certificates = [cert.id],
        hostname     = domain,
    )

    https_proxy = gcp.compute.TargetHttpsProxy(
        "autognosys-https-proxy",
        name            = "autognosys-https-proxy",
        project         = project,
        url_map         = url_map.id,
        certificate_map = pulumi.Output.concat(
            "//certificatemanager.googleapis.com/", cert_map.id),
        opts            = pulumi.ResourceOptions(depends_on=[cert_map_entry]),
    )
    https_rule = gcp.compute.GlobalForwardingRule(
        "autognosys-https-rule",
        name                  = "autognosys-https-rule",
        project               = project,
        target                = https_proxy.id,
        port_range            = "443",
        ip_protocol           = "TCP",
        ip_address            = lb_ip.address,
        load_balancing_scheme = "EXTERNAL_MANAGED",
    )

    # ── Port 80: permanent redirect to HTTPS ──────────────────────────────────
    redirect_map = gcp.compute.URLMap(
        "autognosys-redirect-urlmap",
        name    = "autognosys-redirect-urlmap",
        project = project,
        default_url_redirect = gcp.compute.URLMapDefaultUrlRedirectArgs(
            https_redirect         = True,
            redirect_response_code = "MOVED_PERMANENTLY_DEFAULT",
            strip_query            = False,
        ),
    )
    http_proxy = gcp.compute.TargetHttpProxy(
        "autognosys-http-proxy",
        name    = "autognosys-http-proxy",
        project = project,
        url_map = redirect_map.id,
    )
    http_rule = gcp.compute.GlobalForwardingRule(
        "autognosys-http-rule",
        name                  = "autognosys-http-rule",
        project               = project,
        target                = http_proxy.id,
        port_range            = "80",
        ip_protocol           = "TCP",
        ip_address            = lb_ip.address,
        load_balancing_scheme = "EXTERNAL_MANAGED",
    )

    return lb_ip, dns_auth, https_rule, http_rule, lb_firewall
