# frozen_string_literal: true

require 'openssl'

# Chatwoot v4.7.0 API-inbox webhooks are not HMAC signed by the stock
# Webhooks::Trigger path. Conversation deletion is destructive, so the bridge
# extension signs this one callback explicitly instead of weakening the
# integration receiver.
#
# Deliberately enqueue only the inbox ID and payload. Never serialize the API
# inbox HMAC token into ActiveJob/Sidekiq/Redis, and always resolve the current
# token at execution time so token rotation between enqueue and delivery is safe.
class MetaConversationDeleteWebhookJob < ApplicationJob
  queue_as :medium

  def perform(inbox_id, payload)
    inbox = Inbox.find_by(id: inbox_id)
    if inbox.blank? || inbox.channel_type != 'Channel::Api'
      Rails.logger.warn("conversation.deleted callback suppressed: API inbox #{inbox_id} no longer exists")
      return
    end

    data = payload.with_indifferent_access
    unless data.dig(:inbox, :id).to_i == inbox.id && data.dig(:account, :id).to_i == inbox.account_id
      Rails.logger.error("conversation.deleted callback suppressed: API inbox #{inbox_id} scope changed")
      return
    end

    channel = inbox.channel
    if channel.blank? || channel.webhook_url.blank?
      Rails.logger.warn("conversation.deleted callback suppressed: API inbox #{inbox_id} has no webhook URL")
      return
    end

    secret = channel.hmac_token.to_s
    if secret.blank?
      Rails.logger.error("conversation.deleted callback suppressed: API inbox #{inbox_id} has no HMAC token")
      return
    end

    timestamp = Time.now.to_i.to_s
    body = payload.to_json
    signature = "sha256=#{OpenSSL::HMAC.hexdigest('SHA256', secret, "#{timestamp}.#{body}")}"

    RestClient::Request.execute(
      method: :post,
      url: channel.webhook_url,
      payload: body,
      headers: {
        content_type: :json,
        accept: :json,
        'X-Chatwoot-Timestamp' => timestamp,
        'X-Chatwoot-Signature' => signature
      },
      timeout: 5
    )
  end
end
