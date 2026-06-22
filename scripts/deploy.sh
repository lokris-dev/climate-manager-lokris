#!/usr/bin/env bash
# Deploy delormej_climate to a Home Assistant instance via SSH.
#
# Required env vars:
#   HA_SSH_USER   — SSH user (e.g. root, hassio)
#   HA_SSH_HOST   — SSH host (e.g. 192.168.1.42 or ha.local)
#   HA_SSH_PORT   — SSH port (defaults to 22; HA SSH add-on usually 22222)
#   HA_CONFIG_DIR — remote /config path (defaults to /config)
#
# Optional:
#   HA_RESTART_AFTER_DEPLOY=1   — call /api/services/homeassistant/restart afterwards
#   HASS_SERVER, HASS_TOKEN     — needed if HA_RESTART_AFTER_DEPLOY=1
set -euo pipefail

cd "$(dirname "$0")/.."

: "${HA_SSH_HOST:?Need HA_SSH_HOST (e.g. ha.lan)}"
: "${HA_SSH_USER:?Need HA_SSH_USER (e.g. root)}"
HA_SSH_PORT="${HA_SSH_PORT:-22}"
HA_CONFIG_DIR="${HA_CONFIG_DIR:-/config}"

SRC=custom_components/delormej_climate
TARGET="${HA_CONFIG_DIR}/custom_components/delormej_climate"

echo "==> Deploying ${SRC} → ${HA_SSH_USER}@${HA_SSH_HOST}:${TARGET} (port ${HA_SSH_PORT})"

ssh -p "${HA_SSH_PORT}" "${HA_SSH_USER}@${HA_SSH_HOST}" \
    "mkdir -p '${HA_CONFIG_DIR}/custom_components' && rm -rf '${TARGET}' && mkdir -p '${TARGET}'"

scp -P "${HA_SSH_PORT}" -r "${SRC}/." "${HA_SSH_USER}@${HA_SSH_HOST}:${TARGET}/"

ssh -p "${HA_SSH_PORT}" "${HA_SSH_USER}@${HA_SSH_HOST}" \
    "ls -la '${TARGET}' && echo '---' && python3 -c 'import json; json.load(open(\"${TARGET}/manifest.json\"))' && echo 'manifest.json OK'"

if [[ "${HA_RESTART_AFTER_DEPLOY:-0}" == "1" ]]; then
    : "${HASS_SERVER:?Need HASS_SERVER for restart}"
    : "${HASS_TOKEN:?Need HASS_TOKEN for restart}"
    echo "==> Triggering HA restart via API"
    curl -fsS -X POST -H "Authorization: Bearer ${HASS_TOKEN}" \
        -H "Content-Type: application/json" \
        "${HASS_SERVER}/api/services/homeassistant/restart" >/dev/null
    echo "    restart requested"
fi

echo "==> Done."
