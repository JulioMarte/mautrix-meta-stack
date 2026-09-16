# frozen_string_literal: true

# Chatwoot dispatches an internal conversation.deleted event but v4.7.0 does
# not expose it through API-inbox webhooks. The stock API-inbox delivery path in
# that release is also unsigned, while our destructive integration callback is
# intentionally HMAC-only. This initializer therefore forwards only deletion
# events through a dedicated signed job.
module MetaConversationDeleteWebhook
  def conversation_deleted(event)
    data = event.data[:conversation_data]&.with_indifferent_access
    return if data.blank?

    inbox = Inbox.find_by(id: data[:inbox_id], account_id: data[:account_id])
    return if inbox.blank? || inbox.channel_type != 'Channel::Api'

    channel = inbox.channel
    return if channel.blank? || channel.webhook_url.blank?

    secret = channel.secret.to_s
    if secret.blank?
      Rails.logger.error(
        "conversation.deleted callback suppressed: API inbox #{inbox.id} has no signing secret"
      )
      return
    end

    payload = {
      event: __method__.to_s,
      id: data[:id],
      conversation_id: data[:id],
      account: { id: data[:account_id] },
      inbox: { id: data[:inbox_id] }
    }

    MetaConversationDeleteWebhookJob.perform_later(channel.webhook_url, payload, secret)
  end
end

Rails.application.config.to_prepare do
  WebhookListener.prepend(MetaConversationDeleteWebhook) unless WebhookListener < MetaConversationDeleteWebhook
end
