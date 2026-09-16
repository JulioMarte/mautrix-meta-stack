# Build with the exact Chatwoot image deployed in production. CI currently
# validates the extension against v4.7.0 because that is the documented stack
# baseline for this repository. The default keeps local/CI builds valid while
# still allowing production to override the exact deployed tag.
ARG CHATWOOT_BASE_IMAGE=chatwoot/chatwoot:v4.7.0
FROM ${CHATWOOT_BASE_IMAGE}

COPY app/jobs/meta_conversation_delete_webhook_job.rb \
  /app/app/jobs/meta_conversation_delete_webhook_job.rb
COPY config/initializers/meta_conversation_delete_webhook.rb \
  /app/config/initializers/meta_conversation_delete_webhook.rb
COPY contract_test.rb /app/meta_conversation_delete_contract_test.rb
