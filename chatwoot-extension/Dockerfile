# Build with the exact Chatwoot image deployed in production.
# conversation.deleted is required; Chatwoot v4.15.0 is the oldest release
# explicitly covered by CI in this repository.
# Example:
# docker build --build-arg CHATWOOT_BASE_IMAGE=chatwoot/chatwoot:v4.15.0 .
ARG CHATWOOT_BASE_IMAGE
FROM ${CHATWOOT_BASE_IMAGE}

# Fail the image build instead of silently shipping a deletion extension on a
# Chatwoot release that does not expose the internal deletion event/callbacks we
# depend on. This intentionally checks the upstream application files before the
# local initializer is copied in.
RUN grep -Fq "CONVERSATION_DELETED = 'conversation.deleted'" /app/lib/events/types.rb \
 && grep -Fq "after_destroy_commit :notify_conversation_deletion" /app/app/models/conversation.rb \
 && grep -Fq "def deliver_api_inbox_webhooks" /app/app/listeners/webhook_listener.rb

COPY config/initializers/meta_conversation_delete_webhook.rb \
  /app/config/initializers/meta_conversation_delete_webhook.rb
