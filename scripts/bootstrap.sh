#!/usr/bin/env bash
set -euo pipefail

SYNAPSE_IMAGE="${SYNAPSE_IMAGE:-matrixdotorg/synapse:v1.160.0}"
MAUTRIX_META_IMAGE="${MAUTRIX_META_IMAGE:-dock.mau.dev/mautrix/meta:v26.07}"
STACK_DATA_ROOT="${STACK_DATA_ROOT:-/data/mautrix-meta-stack}"
TZ="${TZ:-America/Santo_Domingo}"

if [[ -z "${MATRIX_SERVER_NAME:-}" ]]; then
  echo "ERROR: MATRIX_SERVER_NAME is required (example: matrix.example.com)" >&2
  exit 1
fi

if [[ -z "${MATRIX_ADMIN_MXID:-}" ]]; then
  echo "ERROR: MATRIX_ADMIN_MXID is required (example: @admin:matrix.example.com)" >&2
  exit 1
fi

if [[ "${MATRIX_ADMIN_MXID}" != *@"${MATRIX_SERVER_NAME}" ]]; then
  echo "ERROR: MATRIX_ADMIN_MXID must belong to MATRIX_SERVER_NAME (${MATRIX_SERVER_NAME})." >&2
  exit 1
fi

SYNAPSE_DIR="${STACK_DATA_ROOT}/synapse"
META_DIR="${STACK_DATA_ROOT}/mautrix-meta"
APPSERVICE_DIR="${SYNAPSE_DIR}/appservices"

mkdir -p "${SYNAPSE_DIR}" "${META_DIR}" "${APPSERVICE_DIR}"

echo "==> Pulling pinned images"
docker pull "${SYNAPSE_IMAGE}"
docker pull "${MAUTRIX_META_IMAGE}"

if [[ ! -f "${SYNAPSE_DIR}/homeserver.yaml" ]]; then
  echo "==> Generating Synapse configuration"
  docker run --rm \
    -e SYNAPSE_SERVER_NAME="${MATRIX_SERVER_NAME}" \
    -e SYNAPSE_REPORT_STATS=no \
    -e TZ="${TZ}" \
    -v "${SYNAPSE_DIR}:/data" \
    "${SYNAPSE_IMAGE}" generate
else
  echo "==> Synapse configuration already exists; leaving generated identity/keys intact"
fi

if [[ ! -f "${META_DIR}/config.yaml" ]]; then
  echo "==> Generating mautrix-meta configuration"
  docker run --rm \
    -v "${META_DIR}:/data" \
    "${MAUTRIX_META_IMAGE}"
else
  echo "==> mautrix-meta config.yaml already exists; applying idempotent stack settings"
fi

echo "==> Configuring mautrix-meta for Synapse + Facebook/Marketplace"
docker run --rm \
  --entrypoint /bin/sh \
  -e MATRIX_SERVER_NAME="${MATRIX_SERVER_NAME}" \
  -e MATRIX_ADMIN_MXID="${MATRIX_ADMIN_MXID}" \
  -v "${META_DIR}:/data" \
  "${MAUTRIX_META_IMAGE}" \
  -ec '
    yq -i '\''
      .network.mode = "facebook" |
      .network.marketplace_space = true |
      .bridge.permissions."*" = "relay" |
      .bridge.permissions[strenv(MATRIX_SERVER_NAME)] = "user" |
      .bridge.permissions[strenv(MATRIX_ADMIN_MXID)] = "admin" |
      .bridge.database.type = "sqlite3-fk-wal" |
      .bridge.database.uri = "file:/data/mautrix-meta.db?_txlock=immediate" |
      .homeserver.address = "http://synapse:8008" |
      .homeserver.domain = strenv(MATRIX_SERVER_NAME) |
      .homeserver.software = "standard" |
      .appservice.address = "http://mautrix-meta:29319" |
      .appservice.public_address = null |
      .appservice.hostname = "0.0.0.0" |
      .appservice.port = 29319
    '\'' /data/config.yaml
  '

if [[ ! -f "${META_DIR}/registration.yaml" ]]; then
  echo "==> Generating mautrix-meta appservice registration"
  docker run --rm \
    -v "${META_DIR}:/data" \
    "${MAUTRIX_META_IMAGE}"
else
  echo "==> registration.yaml already exists; preserving existing appservice tokens"
fi

if [[ ! -f "${META_DIR}/registration.yaml" ]]; then
  echo "ERROR: mautrix-meta did not generate registration.yaml" >&2
  exit 1
fi

cp "${META_DIR}/registration.yaml" "${APPSERVICE_DIR}/mautrix-meta.yaml"
chmod 600 "${APPSERVICE_DIR}/mautrix-meta.yaml"

echo "==> Registering appservice in Synapse configuration"
docker run --rm \
  --entrypoint /bin/sh \
  -v "${SYNAPSE_DIR}:/synapse" \
  "${MAUTRIX_META_IMAGE}" \
  -ec '
    yq -i '\''
      .app_service_config_files = ["/data/appservices/mautrix-meta.yaml"]
    '\'' /synapse/homeserver.yaml
  '

# Official Synapse image defaults to UID/GID 991, and mautrix-meta to 1337.
chown -R 991:991 "${SYNAPSE_DIR}"
chown -R 1337:1337 "${META_DIR}"

cat <<EOF

Bootstrap complete.

Persistent data:
  Synapse:      ${SYNAPSE_DIR}
  mautrix-meta: ${META_DIR}

Next steps:
  1. Set the same STACK_DATA_ROOT and pinned image variables in Coolify.
  2. Deploy compose.yaml from this repository.
  3. Expose ONLY Synapse using your Matrix domain and internal port 8008.
  4. Create the Matrix user ${MATRIX_ADMIN_MXID} after Synapse is healthy.
  5. Log into Matrix and authenticate mautrix-meta with Facebook.

Do not expose mautrix-meta:29319 publicly.
Do not run more than one mautrix-meta replica against this data directory.
EOF
