#!/usr/bin/env bash
set -euo pipefail

MAUTRIX_META_IMAGE="${MAUTRIX_META_IMAGE:-dock.mau.dev/mautrix/meta:v26.07}"
STACK_DATA_ROOT="${STACK_DATA_ROOT:-/data/mautrix-meta-stack}"
SYNAPSE_DIR="${STACK_DATA_ROOT}/synapse"
META_DIR="${STACK_DATA_ROOT}/mautrix-meta"

required_files=(
  "${SYNAPSE_DIR}/homeserver.yaml"
  "${META_DIR}/config.yaml"
  "${META_DIR}/registration.yaml"
)

for file in "${required_files[@]}"; do
  if [[ ! -f "${file}" ]]; then
    echo "MISSING: ${file}" >&2
    exit 1
  fi
done

echo "==> Checking mautrix-meta deployment-critical settings"
docker run --rm \
  --entrypoint /bin/sh \
  -v "${META_DIR}:/data:ro" \
  "${MAUTRIX_META_IMAGE}" \
  -ec '
    test "$(yq -r ".network.mode" /data/config.yaml)" = "facebook"
    test "$(yq -r ".network.marketplace_space" /data/config.yaml)" = "true"
    test "$(yq -r ".homeserver.address" /data/config.yaml)" = "http://synapse:8008"
    test "$(yq -r ".appservice.address" /data/config.yaml)" = "http://mautrix-meta:29319"
    test "$(yq -r ".appservice.hostname" /data/config.yaml)" = "0.0.0.0"
    test "$(yq -r ".database.type" /data/config.yaml)" = "sqlite3-fk-wal"
  '

echo "==> Checking Synapse appservice registration path"
docker run --rm \
  --entrypoint /bin/sh \
  -v "${SYNAPSE_DIR}:/synapse:ro" \
  "${MAUTRIX_META_IMAGE}" \
  -ec '
    yq -e '\''(.app_service_config_files // []) | any(. == "/meta/registration.yaml")'\'' /synapse/homeserver.yaml >/dev/null
  '

echo "OK: stack configuration is structurally ready."
