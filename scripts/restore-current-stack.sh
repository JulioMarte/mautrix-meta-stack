#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-compose.yaml}"
BACKUP_DIR="${BACKUP_DIR:?Set BACKUP_DIR to one backup directory created by backup-current-stack.sh}"
BACKUP_IMAGE="${BACKUP_IMAGE:-alpine:3.22.1}"
CONFIRM_RESTORE="${CONFIRM_RESTORE:-}"
DC=(docker compose -f "$COMPOSE_FILE")

[[ "$CONFIRM_RESTORE" == "RESTORE" ]] || {
  echo "Refusing destructive restore. Set CONFIRM_RESTORE=RESTORE." >&2
  exit 64
}

BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
for file in synapse.tgz mautrix-meta.tgz integration.tgz SHA256SUMS MANIFEST; do
  [[ -s "$BACKUP_DIR/$file" ]] || { echo "Missing backup file: $file" >&2; exit 1; }
done
(cd "$BACKUP_DIR" && sha256sum -c SHA256SUMS)

declare -A SERVICE_DEST=(
  [synapse]="/data"
  [mautrix-meta]="/data"
  [integration]="/data"
)

resolve_volume() {
  local service="$1" destination="$2" cid
  cid="$("${DC[@]}" ps -aq "$service" | head -n1)"
  [[ -n "$cid" ]] || { echo "No container exists for service $service; create the stack once before restore" >&2; return 1; }
  docker inspect "$cid" --format "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{.Name}}{{end}}{{end}}"
}

declare -A VOLUMES=()
for service in synapse mautrix-meta integration; do
  volume="$(resolve_volume "$service" "${SERVICE_DEST[$service]}")"
  [[ -n "$volume" ]] || { echo "Could not resolve target volume for $service" >&2; exit 1; }
  VOLUMES["$service"]="$volume"
done

echo "Stopping state owners before destructive restore"
"${DC[@]}" stop integration mautrix-meta synapse

for service in synapse mautrix-meta integration; do
  volume="${VOLUMES[$service]}"
  archive="$service.tgz"
  echo "Restoring $service into volume $volume"
  # Feed the archive over stdin instead of bind-mounting the backup path.
  # This mirrors backup streaming and avoids host/container ownership quirks.
  docker run --rm -i \
    -v "$volume:/target" \
    "$BACKUP_IMAGE" \
    sh -eu -c "find /target -mindepth 1 -maxdepth 1 -exec rm -rf {} +; tar -C /target -xzf -" \
    < "$BACKUP_DIR/$archive"
done

"${DC[@]}" up -d

echo "Restore completed. Do not send production traffic until /health, /ready and the live bidirectional acceptance checks pass."
