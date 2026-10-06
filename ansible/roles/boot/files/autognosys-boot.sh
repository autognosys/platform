#!/bin/bash
# Boot-time setup for VMs built from the Autognosys Packer image.
#
#   1. Update the app code (git pull; rebuild only if HEAD moved)
#   2. Fetch secrets from GCP Secret Manager and write the env files read by
#      OpenObserve and the OTel Collector
#
# The code update runs first so the site still comes up if secrets fail.
# Runs as root from autognosys-boot.service, which is ordered before
# pm2-ubuntu, openobserve and otelcol-contrib.
#
# Secret values are written to systemd EnvironmentFile format, so avoid
# quotes, backslashes and newlines in them.

set -uo pipefail

LOG=/var/log/autognosys-boot.log
exec >>"$LOG" 2>&1
echo "[boot] $(date -u +%FT%TZ) begin"

APP_DIR=/app/platform
APP_USER=ubuntu
rc=0

retry() {  # retry <attempts> <command...>
  local attempts=$1 i
  shift
  for ((i = 1; i <= attempts; i++)); do
    if "$@"; then
      return 0
    fi
    sleep 3
  done
  return 1
}

as_app_user() {
  sudo -H -u "$APP_USER" "$@"
}

# ── 1. App code ────────────────────────────────────────────────────────────
if [ -d "$APP_DIR/.git" ]; then
  old=$(as_app_user git -C "$APP_DIR" rev-parse HEAD)
  if retry 5 as_app_user git -C "$APP_DIR" pull --ff-only; then
    new=$(as_app_user git -C "$APP_DIR" rev-parse HEAD)
    if [ "$old" != "$new" ]; then
      echo "[boot] code changed ${old:0:7} -> ${new:0:7}; rebuilding"
      as_app_user bash -c "cd '$APP_DIR/website' && npm ci && npm run build" || rc=1
      as_app_user bash -c "cd '$APP_DIR/backend' && /usr/local/bin/uv sync" || rc=1
    else
      echo "[boot] code already at ${new:0:7}"
    fi
  else
    echo "[boot] WARNING: git pull failed; continuing with the baked code"
    rc=1
  fi
else
  echo "[boot] WARNING: $APP_DIR is not a git checkout"
  rc=1
fi

# ── 2. Secrets from Secret Manager ─────────────────────────────────────────
MD=http://metadata.google.internal/computeMetadata/v1

md() {  # md <metadata path>
  curl -fsS -H 'Metadata-Flavor: Google' "$MD/$1"
}

fetch_token() {
  md instance/service-accounts/default/token |
    python3 -c 'import sys, json; print(json.load(sys.stdin)["access_token"])'
}

get_secret() {  # get_secret <secret name>
  curl -fsS -H "Authorization: Bearer $TOKEN" \
    "https://secretmanager.googleapis.com/v1/projects/$PROJECT/secrets/$1/versions/latest:access" |
    python3 -c 'import sys, json, base64; sys.stdout.write(base64.b64decode(json.load(sys.stdin)["payload"]["data"]).decode())'
}

if PROJECT=$(retry 5 md project/project-id) &&
  TOKEN=$(retry 5 fetch_token) &&
  EMAIL=$(retry 5 get_secret openobserve-admin-email) &&
  PASSWORD=$(retry 5 get_secret openobserve-admin-password); then
  umask 077
  install -d -m 0750 /etc/openobserve
  printf 'ZO_ROOT_USER_EMAIL=%s\nZO_ROOT_USER_PASSWORD=%s\n' "$EMAIL" "$PASSWORD" \
    >/etc/openobserve/openobserve.env
  install -d -m 0755 /etc/otelcol-contrib
  printf 'OPENOBSERVE_BASIC_AUTH=%s\n' "$(printf '%s:%s' "$EMAIL" "$PASSWORD" | base64 -w0)" \
    >/etc/otelcol-contrib/openobserve.env
  echo "[boot] secrets written"
else
  echo "[boot] ERROR: could not fetch secrets from Secret Manager"
  rc=1
fi

echo "[boot] $(date -u +%FT%TZ) done rc=$rc"
exit "$rc"
