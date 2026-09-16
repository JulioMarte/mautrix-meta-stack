# Build with the exact Chatwoot image already deployed, for example:
# docker build --build-arg CHATWOOT_BASE_IMAGE=chatwoot/chatwoot:v4.7.0 .
ARG CHATWOOT_BASE_IMAGE
FROM ${CHATWOOT_BASE_IMAGE}

COPY config/initializers/meta_conversation_delete_webhook.rb \
  /app/config/initializers/meta_conversation_delete_webhook.rb
