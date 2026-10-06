"""Pulumi resources for the preemption auto-remediation Cloud Function.

Import this from infra/__main__.py and call create_auto_remediation()
with the same project/region already in scope there, so there's a
single source of truth instead of two independent config reads.
"""

import pulumi
import pulumi_gcp as gcp


def create_auto_remediation(project: str, region: str) -> gcp.cloudfunctionsv2.Function:
    """Create the service account, IAM bindings, and Cloud Function/
    Eventarc trigger that starts a preempted instance directly (bypassing
    the MIG's own startInstances, which collides with itself on autoheal).

    Returns the Function resource so the caller can export its name.
    """

    # --- Service account -------------------------------------------
    # Scoped narrowly: compute.instanceAdmin.v1 is enough to call
    # instances().start(); it does not grant network/firewall edit rights.
    sa = gcp.serviceaccount.Account(
        "auto-remediation-sa",
        account_id="auto-remediation",
        display_name="Preemption auto-remediation Cloud Function",
        project=project,
    )
    sa_member = sa.email.apply(lambda email: f"serviceAccount:{email}")

    compute_binding = gcp.projects.IAMMember(
        "auto-remediation-compute-instance-admin",
        project=project,
        role="roles/compute.instanceAdmin.v1",
        member=sa_member,
    )

    eventarc_binding = gcp.projects.IAMMember(
        "auto-remediation-eventarc-receiver",
        project=project,
        role="roles/eventarc.eventReceiver",
        member=sa_member,
    )

    logging_binding = gcp.projects.IAMMember(
        "auto-remediation-log-writer",
        project=project,
        role="roles/logging.logWriter",
        member=sa_member,
    )

    # --- Source upload -----------------------------------------------
    bucket = gcp.storage.Bucket(
        "auto-remediation-src",
        project=project,
        location=region,
        uniform_bucket_level_access=True,
    )

    source_archive = gcp.storage.BucketObject(
        "auto-remediation-src-zip",
        bucket=bucket.name,
        # Points at the directory containing main.py + requirements.txt,
        # relative to wherever `pulumi up` is run from (infra/).
        source=pulumi.FileArchive("./cloud-function"),
    )

    # --- Log sink + Pub/Sub topic ---------------------------------------
    # System Event audit logs (which compute.instances.preempted is) are
    # not reliably delivered via Eventarc's audit-log trigger type -- that
    # type is documented for Admin Activity / Data Access logs. The robust,
    # standard pattern is a dedicated log sink filtered to the exact event,
    # routed to our own Pub/Sub topic, consumed by a Pub/Sub-triggered
    # function instead.
    topic = gcp.pubsub.Topic(
        "preemption-notifications",
        project=project,
        name="preemption-notifications",
    )

    sink = gcp.logging.ProjectSink(
        "preemption-sink",
        project=project,
        destination=topic.id.apply(lambda tid: f"pubsub.googleapis.com/{tid}"),
        filter='resource.type="gce_instance" AND protoPayload.methodName="compute.instances.preempted"',
        unique_writer_identity=True,
    )

    # The sink writes as its own auto-generated identity, which needs
    # publish rights on the topic it's pushing into.
    sink_publisher_binding = gcp.pubsub.TopicIAMMember(
        "preemption-sink-publisher",
        project=project,
        topic=topic.name,
        role="roles/pubsub.publisher",
        member=sink.writer_identity,
    )

    # --- Cloud Function (2nd gen), Pub/Sub-triggered ------------------------
    function = gcp.cloudfunctionsv2.Function(
        "auto-remediation",
        project=project,
        location=region,
        build_config=gcp.cloudfunctionsv2.FunctionBuildConfigArgs(
            runtime="python312",
            entry_point="handle_preemption",
            source=gcp.cloudfunctionsv2.FunctionBuildConfigSourceArgs(
                storage_source=gcp.cloudfunctionsv2.FunctionBuildConfigSourceStorageSourceArgs(
                    bucket=bucket.name,
                    object=source_archive.name,
                ),
            ),
        ),
        service_config=gcp.cloudfunctionsv2.FunctionServiceConfigArgs(
            max_instance_count=1,
            available_memory="512M",
            timeout_seconds=480,
            service_account_email=sa.email,
        ),
        event_trigger=gcp.cloudfunctionsv2.FunctionEventTriggerArgs(
            trigger_region=region,
            event_type="google.cloud.pubsub.topic.v1.messagePublished",
            pubsub_topic=topic.id,
            service_account_email=sa.email,
            # RETRY: main.py now re-raises on genuinely transient failures
            # (e.g. ZONE_RESOURCE_POOL_EXHAUSTED stockout) rather than
            # swallowing every error, so retries here are meaningful --
            # Pub/Sub's default backoff/retention handles the retry
            # schedule. Only the "already running" case (not actually a
            # failure) returns normally and skips this path.
            retry_policy="RETRY_POLICY_RETRY",
        ),
        opts=pulumi.ResourceOptions(
            depends_on=[compute_binding, eventarc_binding, logging_binding, sink_publisher_binding]
        ),
    )

    # Cloud Functions v2 normally auto-grants run.invoker to the trigger's
    # service account as part of Eventarc trigger provisioning -- but that
    # doesn't reliably happen when the Pub/Sub topic is self-managed (ours)
    # rather than auto-created by Eventarc. Grant it explicitly so delivery
    # doesn't silently 403 on every attempt.
    invoker_binding = gcp.cloudrunv2.ServiceIamMember(
        "auto-remediation-run-invoker",
        project=project,
        location=region,
        name=function.name,
        role="roles/run.invoker",
        member=sa_member,
        opts=pulumi.ResourceOptions(depends_on=[function]),
    )

    return function
