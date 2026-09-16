# frozen_string_literal: true

require 'openssl'

# Chatwoot v4.7.0 API-inbox webhooks are not HMAC signed by the stock
# Webhooks::Trigger path. Conversation deletion is destructive, so the bridge
# extension signs this one callback explicitly instead of weakening the
# integration receiver.
class MetaConversationDeleteWebhookJob < ApplicationJob
  queue_as :medium

  def perform(url, payload, secret)
    raise ArgumentError, 'conversation delete webhook URL is missing' if url.blank?
    raise ArgumentError, 'conversation delete webhook secret is missing' if secret.blank?

    timestamp = Time.now.to_i.to_s
    body = payload.to_json
    signature = "sha256=#{OpenSSL::HMAC.hexdigest('SHA256', secret, "#{timestamp}.#{body}")}"

    RestClient::Request.execute(
      method: :post,
      url: url,
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
