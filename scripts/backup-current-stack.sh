#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-compose.yaml}"
BACKUP_ROOT="${BACKUP_ROOT:?Set BACKUP_ROOT to an off-container backup directory}"
BACKUP_IMAGE="${BACKUP_IMAGE:-alpine:3.22.1}"
STAMP="${BACKUP_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
DEST="$(mkdir -p "$BACKUP_ROOT" && cd "$BACKUP_ROOT" && pwd)/$STAMP"
DC=(docker compose -f "$COMPOSE_FILE")

mkdir -p "$DEST"
chmod 700 "$DEST"

declare -A SERVICE_DEST=(
  [synapse]="/data"
  [mautrix-meta]="/data"
  [integration]="/data"
)

resolve_volume() {
  local service="$1" destination="$2" cid
  cid="$("${DC[@]}" ps -aq "$service" | head -n1)"
  [[ -n "$cid" ]] || { echo "No container exists for service $service; run docker compose up at least once" >&2; return 1; }
  docker inspect "$cid" --format "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{.Name}}{{end}}{{end}}"
}

declare -A VOLUMES=()
for service in synapse mautrix-meta integration; do
  volume="$(resolve_volume "$service" "${SERVICE_DEST[$service]}")"
  [[ -n "$volume" ]] || { echo "Could not resolve persistent volume for $service" >&2; exit 1; }
  VOLUMES["$service"]="$volume"
done

echo "Stopping SQLite/state owners for a consistent cold backup"
"${DC[@]}" stop integration mautrix-meta synapse

restart_stack() {
  "${DC[@]}" up -d >/dev/null 2>&1 || true
}
trap restart_stack EXIT

for service in synapse mautrix-meta integration; do
  archive="$DEST/$service.tgz"
  volume="${VOLUMES[$service]}"
  echo "Backing up $service from volume $volume"
  # Stream the archive through stdout so the host shell creates the file.
  # This avoids Docker-root ownership on bind-mounted backup directories and
  # makes the script work on rootless/hosted runners as well as normal VPSes.
  docker run --rm \
    -v "$volume:/source:ro" \
    "$BACKUP_IMAGE" \
    sh -eu -c "tar -C /source -czf - ." > "$archive"
  [[ -s "$archive" ]] || { echo "Backup archive is empty: $archive" >&2; exit 1; }
  chmod 600 "$archive"
done

(
  cd "$DEST"
  sha256sum synapse.tgz mautrix-meta.tgz integration.tgz > SHA256SUMS
  chmod 600 SHA256SUMS
  {
    echo "created_at_utc=$STAMP"
    echo "compose_file=$COMPOSE_FILE"
    echo "git_revision=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "synapse_volume=${VOLUMES[synapse]}"
    echo "mautrix_meta_volume=${VOLUMES[mautrix-meta]}"
    echo "integration_volume=${VOLUMES[integration]}"
  } > MANIFEST
  chmod 600 MANIFEST
)

trap - EXIT
"${DC[@]}" up -d

echo "Cold backup complete: $DEST"
echo "Copy this directory off the VPS and test a restore before production traffic."
