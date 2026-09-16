FROM golang:1-alpine3.24 AS builder

ARG MAUTRIX_META_UPSTREAM_SHA=001f276beca5b90dead1bbc1351e1036e3f966a7

# Coolify deployment hosts can be substantially smaller than GitHub-hosted
# runners. Keep the patched source build deterministic while bounding compiler
# parallelism and Go heap growth so a deployment cannot be killed mid-build by
# host memory pressure with only an opaque `docker exec` exit 255.
ENV GOMAXPROCS=1 \
    GOFLAGS="-p=1" \
    GOMEMLIMIT=768MiB

RUN apk add --no-cache git ca-certificates build-base su-exec olm-dev python3

WORKDIR /src
RUN git clone --filter=blob:none https://github.com/mautrix/meta.git mautrix-meta \
    && git -C mautrix-meta checkout --detach "$MAUTRIX_META_UPSTREAM_SHA" \
    && test "$(git -C mautrix-meta rev-parse HEAD)" = "$MAUTRIX_META_UPSTREAM_SHA"

COPY mautrix-meta-runtime/apply_thread_backfill_refresh.py /patch/apply_thread_backfill_refresh.py
RUN python3 /patch/apply_thread_backfill_refresh.py /src/mautrix-meta \
    && cd /src/mautrix-meta \
    && gofmt -w pkg/connector/client.go pkg/connector/threadbackfill.go pkg/messagix/client.go \
    && git diff --check \
    && ./build.sh

FROM alpine:3.24

ENV UID=1337 \
    GID=1337 \
    MAUTRIX_META_RUNTIME_BASE=v0.2608.1 \
    MAUTRIX_META_THREAD_REFRESH=once-per-process

RUN apk add --no-cache ffmpeg su-exec ca-certificates olm bash jq yq-go curl

COPY --from=builder /src/mautrix-meta/mautrix-meta /usr/bin/mautrix-meta
COPY --from=builder /src/mautrix-meta/docker-run.sh /docker-run.sh
VOLUME /data

CMD ["/docker-run.sh"]
