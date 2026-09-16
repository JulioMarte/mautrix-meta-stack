FROM golang:1-alpine3.24 AS builder

ARG MAUTRIX_META_UPSTREAM_SHA=ed37c9e6ce47e83dc75b9abea7b636302715b9bc

RUN apk add --no-cache git ca-certificates build-base su-exec olm-dev python3

WORKDIR /src
RUN git clone --filter=blob:none https://github.com/mautrix/meta.git mautrix-meta \
    && git -C mautrix-meta checkout --detach "$MAUTRIX_META_UPSTREAM_SHA" \
    && test "$(git -C mautrix-meta rev-parse HEAD)" = "$MAUTRIX_META_UPSTREAM_SHA"

COPY mautrix-meta-fork/apply_thread_backfill_refresh.py /patch/apply_thread_backfill_refresh.py
RUN python3 /patch/apply_thread_backfill_refresh.py /src/mautrix-meta \
    && cd /src/mautrix-meta \
    && gofmt -w pkg/connector/client.go pkg/connector/threadbackfill.go \
    && git diff --check \
    && ./build.sh

FROM alpine:3.24

ENV UID=1337 \
    GID=1337

RUN apk add --no-cache ffmpeg su-exec ca-certificates olm bash jq yq-go curl

COPY --from=builder /src/mautrix-meta/mautrix-meta /usr/bin/mautrix-meta
COPY --from=builder /src/mautrix-meta/docker-run.sh /docker-run.sh
VOLUME /data

CMD ["/docker-run.sh"]
