# frozen_string_literal: true

# Chatwoot v4.7.0 does not define a conversation.deleted dispatcher event.
# Its API controller enqueues DeleteObjectJob for a Conversation and that job
# performs object.destroy!. Hook that real deletion path instead of relying on
# an event that does not exist in this pinned Chatwoot release.
#
# The callback is enqueued only AFTER Chatwoot successfully destroys the
# conversation. Only inbox ID + non-secret scope metadata are serialized. The
# delivery job resolves the current API inbox webhook_url and hmac_token at
# execution time, keeping secrets out of Sidekiq/Redis and making token rotation
# safe between deletion and callback delivery.
module MetaConversationDeleteObjectJobHook
  def perform(object, user = nil, ip = nil)
    unless object.is_a?(Conversation)
      return super
    end

    payload = {
      event: 'conversation_deleted',
      id: object.id,
      conversation_id: object.id,
      account: { id: object.account_id },
      inbox: { id: object.inbox_id }
    }
    inbox_id = object.inbox_id

    result = super
    MetaConversationDeleteWebhookJob.perform_later(inbox_id, payload)
    result
  end
end

Rails.application.config.to_prepare do
  DeleteObjectJob.prepend(MetaConversationDeleteObjectJobHook) unless DeleteObjectJob < MetaConversationDeleteObjectJobHook
end
