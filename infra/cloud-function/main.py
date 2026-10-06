"""Auto-remediation Cloud Function for autognosys-net spot VM preemption.

Trigger: Pub/Sub, via a dedicated log sink filtered to
`compute.instances.preempted` (a System Event audit log entry).

Note: this does NOT use Eventarc's audit-log trigger type
(google.cloud.audit.log.v1.written). That type is documented and
reliable for Admin Activity / Data Access audit logs, but System
Event logs -- which preemption notices are -- are not reliably
delivered through it in practice. The robust pattern is a custom
log sink -> our own Pub/Sub topic -> a plain Pub/Sub-triggered
function, which is what this is wired to (see auto_remediation.py).

Why the direct-start logic exists: the MIG's own health-check-based
autoheal action calls instanceGroupManagers().startInstances(), which
collides with itself after a STOP-policy preemption (see
autoremediation thread notes). The fix is to bypass the MIG entirely
and call instances().start() directly on the instance.
"""

import base64
import json
import logging
import time
import urllib.request
from datetime import datetime, timezone

import functions_framework
from cloudevents.http import CloudEvent
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import compute_v1

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("auto-remediation")

PREEMPTED_METHOD = "compute.instances.preempted"
COMPUTE_SERVICE = "compute.googleapis.com"

HEALTH_URL = "https://autognosys.net/api/health"
HEALTH_TIMEOUT_SECONDS = 300
HEALTH_POLL_INTERVAL_SECONDS = 5

# Wait this long for the start operation to finish before giving up.
# The function's own timeout (service_config.timeout_seconds in the
# Pulumi definition) must be comfortably larger than this.
START_OP_TIMEOUT_SECONDS = 100

def _parse_ts(value):
    """Parse an RFC3339 log timestamp (py3.11+ handles 'Z' and nanoseconds)."""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _wait_until_healthy():
    """Poll HEALTH_URL until it returns 200. Never raises; returns the
    time it first succeeded, or None on timeout."""
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
                if resp.status == 200:
                    return datetime.now(timezone.utc)
        except Exception:
            pass
        time.sleep(HEALTH_POLL_INTERVAL_SECONDS)
    return None

def _parse_resource_name(resource_name: str) -> tuple[str, str, str] | None:
    """Parse 'projects/<p>/zones/<z>/instances/<i>' -> (project, zone, instance)."""
    parts = resource_name.split("/")
    try:
        project = parts[parts.index("projects") + 1]
        zone = parts[parts.index("zones") + 1]
        instance = parts[parts.index("instances") + 1]
    except (ValueError, IndexError):
        return None
    return project, zone, instance


@functions_framework.cloud_event
def handle_preemption(cloud_event: CloudEvent) -> None:
    """Entry point registered as the Cloud Function's entrypoint.

    For a Pub/Sub-triggered function, cloud_event.data looks like:
        {"message": {"data": "<base64-encoded LogEntry JSON>", ...},
         "subscription": "..."}
    The log sink's filter already restricts delivery to
    compute.instances.preempted events, but we still defensively
    check the fields after decoding.
    """
    message = (cloud_event.data or {}).get("message", {})
    raw_data = message.get("data", "")

    try:
        log_entry = json.loads(base64.b64decode(raw_data).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.error("Could not decode Pub/Sub message data: %s", exc)
        return

    payload = log_entry.get("protoPayload", {})
    preempted_at = _parse_ts(log_entry.get("timestamp"))

    service_name = payload.get("serviceName", "")
    method_name = payload.get("methodName", "")

    if service_name != COMPUTE_SERVICE or method_name != PREEMPTED_METHOD:
        # The log sink filter already restricts to this exact event;
        # kept as a defensive no-op rather than an error so a filter
        # change upstream doesn't cause noisy failures.
        logger.info(
            "Ignoring event: serviceName=%s methodName=%s",
            service_name, method_name,
        )
        return

    resource_name = payload.get("resourceName", "")
    parsed = _parse_resource_name(resource_name)
    if parsed is None:
        logger.error("Could not parse resourceName: %r", resource_name)
        return

    project, zone, instance = parsed
    client = compute_v1.InstancesClient()

    # Check current status first. Pub/Sub is at-least-once, so this
    # function can genuinely be invoked twice for one preemption event --
    # without this check, two concurrent start() calls can collide with
    # each other's in-flight fingerprint change and BOTH fail, leaving
    # the instance stuck (observed in practice: two calls 6.5s apart,
    # both swallowed as "non-fatal", instance never actually started).
    try:
        current = client.get(project=project, zone=zone, instance=instance)
    except GoogleAPICallError as exc:
        logger.error(
            "Could not fetch status for %s/%s: %s: %s -- retryable, re-raising.",
            zone, instance, type(exc).__name__, exc,
        )
        raise

    if current.status in ("RUNNING", "STAGING", "PROVISIONING"):
        logger.info(
            "Instance %s/%s is already %s -- nothing to do.",
            zone, instance, current.status,
        )
        return

    logger.info(
        "Preemption detected: project=%s zone=%s instance=%s (status=%s). "
        "Issuing direct instances.start() (bypassing the MIG).",
        project, zone, instance, current.status,
    )

    try:
        operation = client.start(project=project, zone=zone, instance=instance)
        operation.result(timeout=START_OP_TIMEOUT_SECONDS)
    except GoogleAPICallError as exc:
        # The pre-check above already ruled out the common "already
        # running" case, so a failure here (including a BadRequest like
        # a fingerprint-changed conflict, or a stockout) is a genuine
        # problem the desired end state wasn't reached. Re-raise so
        # Pub/Sub's retry-with-backoff tries again rather than silently
        # leaving the instance down.
        logger.error(
            "start() for %s/%s raised %s: %s -- retryable, re-raising.",
            zone, instance, type(exc).__name__, exc,
        )
        raise

    started_at = datetime.now(timezone.utc)
    logger.info("Start request for %s/%s completed.", zone, instance)

    healthy_at = _wait_until_healthy()
    record = {
        "severity": "INFO" if healthy_at else "WARNING",
        "message": "recovery measured" if healthy_at else "recovery timed out",
        "event": "recovery",
        "instance": instance,
        "preempted_at": preempted_at.isoformat() if preempted_at else None,
        "start_completed_at": started_at.isoformat(),
        "healthy_at": healthy_at.isoformat() if healthy_at else None,
    }
    if healthy_at and preempted_at:
        record["mttr_seconds"] = round((healthy_at - preempted_at).total_seconds(), 1)
        record["vm_start_seconds"] = round((started_at - preempted_at).total_seconds(), 1)
        record["boot_to_healthy_seconds"] = round((healthy_at - started_at).total_seconds(), 1)
    print(json.dumps(record), flush=True)
