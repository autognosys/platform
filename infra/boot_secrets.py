"""Secret Manager secrets and a dedicated service account for the VM boot script.

Secret *values* are never managed here, so they stay out of Pulumi state. Add
them once with gcloud (see the PR description), e.g.:

    printf '%s' 'admin@example.com' | gcloud secrets versions add \
        openobserve-admin-email --data-file=-
"""

import pulumi
import pulumi_gcp as gcp

SECRET_IDS = ["openobserve-admin-email", "openobserve-admin-password"]


def create_boot_secrets(project: str):
    api = gcp.projects.Service(
        "secretmanager-api",
        project=project,
        service="secretmanager.googleapis.com",
        disable_on_destroy=False,
    )

    # Dedicated identity for the VM instead of the broad default compute account.
    # Attached to the instance template in a follow-up change.
    vm_service_account = gcp.serviceaccount.Account(
        "autognosys-vm-sa",
        project=project,
        account_id="autognosys-vm",
        display_name="Autognosys VM (reads boot-time secrets)",
    )

    secrets = {}
    for secret_id in SECRET_IDS:
        secret = gcp.secretmanager.Secret(
            secret_id,
            project=project,
            secret_id=secret_id,
            replication=gcp.secretmanager.SecretReplicationArgs(
                auto=gcp.secretmanager.SecretReplicationAutoArgs(),
            ),
            opts=pulumi.ResourceOptions(depends_on=[api]),
        )
        gcp.secretmanager.SecretIamMember(
            f"{secret_id}-vm-accessor",
            project=project,
            secret_id=secret.secret_id,
            role="roles/secretmanager.secretAccessor",
            member=vm_service_account.email.apply(lambda email: f"serviceAccount:{email}"),
        )
        secrets[secret_id] = secret

    return vm_service_account, secrets
