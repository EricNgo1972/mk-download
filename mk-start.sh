#!/bin/bash
# Entrypoint of the mk-download tenant container: checks the provisioner's contract, then becomes the service.
#
# `exec` makes the service PID 1, so `docker stop`'s SIGTERM reaches it directly: running jobs are interrupted
# cleanly (live recordings close their segment) and resume after the restart.
set -euo pipefail

log() { echo "mk-start: $*" >&2; }

# The tenant is public through the tunnel. Without both secrets every route would be open, so refuse to start:
# the container exits, the provisioner's health check fails and says why in `docker logs`.
token="${MCP_TOKEN:-}"
password="${ADMIN_PASSWORD:-}"
if [ "${#token}" -lt 16 ]; then
    log "MCP_TOKEN is missing or shorter than 16 characters; refusing to start an open server."
    log "Declare it on the product (secret), give this tenant a value, then Redeploy."
    exit 64
fi
if [ "${#password}" -lt 16 ]; then
    log "ADMIN_PASSWORD is missing or shorter than 16 characters; refusing to start an open settings page."
    log "Declare it on the product (secret), give this tenant a value, then Redeploy."
    exit 64
fi
if [ "$token" = "$password" ]; then
    log "MCP_TOKEN and ADMIN_PASSWORD must differ."
    exit 64
fi

data="${DATA_DIR:-/data}"
mkdir -p "$data"
log "tenant=${TENANT_ID:-?} data=$data webhooks=$([ -n "${WEBHOOK_SECRET:-}" ] && echo on || echo off) proxy=$([ -n "${PROXY_URL:-}" ] && echo on || echo off)"

if [ "$(id -u)" = "0" ]; then
    # The host creates the mounted folder as root; the service runs as `app`.
    if [ "$(stat -c %u "$data")" != "1000" ]; then
        log "giving $data to the app user"
        chown -R app:app "$data"
    fi
    exec env HOME=/home/app setpriv --reuid=app --regid=app --init-groups python -m mkdownload "$@"
fi
exec python -m mkdownload "$@"
