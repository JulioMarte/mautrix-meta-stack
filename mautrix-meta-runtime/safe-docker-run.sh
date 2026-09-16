#!/bin/sh
set -eu

CONFIG=/data/config.yaml

if [ -s "$CONFIG" ]; then
    # Never allow an unlimited thread-list crawl in this deployment. The official
    # connector documents -1 as unlimited; this runtime deliberately replaces it
    # with a small bounded page count and a larger inter-page delay.
    batch_count=2
    batch_delay=20s

    resolver="$(yq -r '.network.get_proxy_from // ""' "$CONFIG" 2>/dev/null || true)"
    if [ -n "$resolver" ]; then
        # The resolver is the same authenticated internal endpoint used by mautrix.
        # We only distinguish DIRECT from an active configured proxy. This does not
        # attempt to certify that a proxy is residential or make any ban-avoidance
        # guarantee.
        proxy_url="$(curl -fsS --max-time 5 "$resolver" 2>/dev/null | jq -r '.proxy_url // ""' 2>/dev/null || true)"
        if [ -n "$proxy_url" ]; then
            batch_count=5
            batch_delay=10s
        fi
    fi

    yq -i ".network.thread_backfill.batch_count = ${batch_count} | .network.thread_backfill.batch_delay = \"${batch_delay}\"" "$CONFIG"
    echo "mautrix-meta thread rediscovery safety: batches=${batch_count} delay=${batch_delay}"
fi

exec /docker-run-upstream.sh "$@"
