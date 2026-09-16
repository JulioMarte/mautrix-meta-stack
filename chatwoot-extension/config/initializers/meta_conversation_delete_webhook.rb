# frozen_string_literal: true

# Chatwoot has an internal conversation.deleted dispatcher event, but the stock
# WebhookListener does not forward it to API inbox callbacks. This initializer adds
# only that missing forwarding path and reuses Chatwoot's existing signed API-inbox
# webhook delivery mechanism.
module MetaConversationDeleteWebhook
  def conversation_deleted(event)
    data = event.data[:conversation_data]&.with_indifferent_access
    return if data.blank?

    inbox = Inbox.find_by(id: data[:inbox_id], account_id: data[:account_id])
    return if inbox.blank? || inbox.channel_type != 'Channel::Api'

    payload = {
      event: __method__.to_s,
      id: data[:id],
      conversation_id: data[:id],
      account: { id: data[:account_id] },
      inbox: { id: data[:inbox_id] }
    }

    # Private in WebhookListener, intentionally reused so signing, retries,
    # delivery_id generation and API-inbox secret handling remain stock Chatwoot.
    deliver_api_inbox_webhooks(payload, inbox)
  end
end

Rails.application.config.to_prepare do
  WebhookListener.prepend(MetaConversationDeleteWebhook) unless WebhookListener < MetaConversationDeleteWebhook
end
