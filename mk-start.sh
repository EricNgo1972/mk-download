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
    # The service runs as `app`. The mounted folder itself belongs to the provisioner, which chmods it on every
    # redeploy: never change its owner, or that chmod fails ("Operation not permitted") and the tenant can no
    # longer be redeployed. Make it writable instead, and give only what is inside it to `app`.
    if ! setpriv --reuid=app --regid=app --init-groups test -w "$data"; then
        log "making $data writable for the app user"
        chmod a+rwx "$data"
    fi
    find "$data" -mindepth 1 \( ! -user app -o ! -group app \) -exec chown -h app:app {} +
    exec env HOME=/home/app setpriv --reuid=app --regid=app --init-groups python -m mkdownload "$@"
fi
exec python -m mkdownload "$@"
