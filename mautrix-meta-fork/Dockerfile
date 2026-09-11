FROM golang:1-alpine3.24 AS builder

ARG MAUTRIX_META_UPSTREAM_SHA=ed37c9e6ce47e83dc75b9abea7b636302715b9bc

RUN apk add --no-cache git ca-certificates build-base su-exec olm-dev python3

WORKDIR /src
RUN git clone --filter=blob:none https://github.com/mautrix/meta.git mautrix-meta \
    && git -C mautrix-meta checkout --detach "$MAUTRIX_META_UPSTREAM_SHA" \
    && test "$(git -C mautrix-meta rev-parse HEAD)" = "$MAUTRIX_META_UPSTREAM_SHA"

COPY mautrix-meta-fork/apply_patch.py /patch/apply_patch.py
COPY mautrix-meta-fork/apply_phase3_fixups.py /patch/apply_phase3_fixups.py
COPY mautrix-meta-fork/apply_phase3_transport_tests.py /patch/apply_phase3_transport_tests.py
COPY mautrix-meta-fork/apply_phase3_resolver_hardening.py /patch/apply_phase3_resolver_hardening.py
RUN python3 /patch/apply_patch.py /src/mautrix-meta \
    && python3 /patch/apply_phase3_fixups.py /src/mautrix-meta \
    && python3 /patch/apply_phase3_transport_tests.py /src/mautrix-meta \
    && python3 /patch/apply_phase3_resolver_hardening.py /src/mautrix-meta \
    && cd /src/mautrix-meta \
    && gofmt -w \
        pkg/connector/client.go \
        pkg/connector/config.go \
        pkg/connector/connector.go \
        pkg/connector/login.go \
        pkg/connector/directmedia.go \
        pkg/connector/events.go \
        pkg/connector/userinfo.go \
        pkg/connector/chatinfo.go \
        pkg/connector/handlemeta.go \
        pkg/connector/proxy_context_test.go \
        pkg/msgconv/mediadl/contextkey.go \
        pkg/msgconv/mediadl/download.go \
        pkg/msgconv/mediadl/proxy_context_test.go \
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
