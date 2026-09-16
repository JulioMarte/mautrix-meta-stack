#!/usr/bin/env sh
set -eu

IMAGE="${CHATWOOT_EXTENSION_IMAGE:-mautrix-meta-chatwoot-delete-contract:v4.7.0}"
BASE_IMAGE="${CHATWOOT_BASE_IMAGE:-chatwoot/chatwoot:v4.7.0}"
NETWORK="chatwoot-delete-contract-${GITHUB_RUN_ID:-local}-$$"
POSTGRES="cw-delete-postgres-$$"
REDIS="cw-delete-redis-$$"

cleanup() {
  docker rm -f "$POSTGRES" "$REDIS" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker build \
  --build-arg "CHATWOOT_BASE_IMAGE=$BASE_IMAGE" \
  -t "$IMAGE" \
  ./chatwoot-extension

docker network create "$NETWORK" >/dev/null

docker run -d --name "$POSTGRES" --network "$NETWORK" \
  -e POSTGRES_DB=chatwoot \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=contract-password \
  postgres:16-alpine >/dev/null

docker run -d --name "$REDIS" --network "$NETWORK" redis:7-alpine >/dev/null

end=$(( $(date +%s) + 60 ))
until docker exec "$POSTGRES" pg_isready -U postgres -d chatwoot >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "$end" ]; then
    echo "Postgres did not become ready" >&2
    exit 1
  fi
  sleep 1
done

COMMON_ENV="-e RAILS_ENV=production -e NODE_ENV=production -e INSTALLATION_ENV=docker -e SECRET_KEY_BASE=contract-secret-key-base-not-for-production -e FRONTEND_URL=http://localhost -e POSTGRES_HOST=$POSTGRES -e POSTGRES_DATABASE=chatwoot -e POSTGRES_USERNAME=postgres -e POSTGRES_PASSWORD=contract-password -e REDIS_URL=redis://$REDIS:6379"

# shellcheck disable=SC2086
docker run --rm --network "$NETWORK" $COMMON_ENV "$IMAGE" \
  bundle exec rails db:chatwoot_prepare

# Boot the exact Chatwoot runtime with our copied job/initializer and assert both
# listener wiring and the raw-body HMAC contract expected by the integration.
# shellcheck disable=SC2086
docker run --rm --network "$NETWORK" $COMMON_ENV "$IMAGE" \
  bundle exec rails runner /app/meta_conversation_delete_contract_test.rb
