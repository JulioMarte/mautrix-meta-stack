# Build with the exact Chatwoot image deployed in production. CI currently
# validates the extension against v4.7.0 because that is the documented stack
# baseline for this repository.
ARG CHATWOOT_BASE_IMAGE
FROM ${CHATWOOT_BASE_IMAGE}

COPY app/jobs/meta_conversation_delete_webhook_job.rb \
  /app/app/jobs/meta_conversation_delete_webhook_job.rb
COPY config/initializers/meta_conversation_delete_webhook.rb \
  /app/config/initializers/meta_conversation_delete_webhook.rb
COPY contract_test.rb /app/meta_conversation_delete_contract_test.rb
