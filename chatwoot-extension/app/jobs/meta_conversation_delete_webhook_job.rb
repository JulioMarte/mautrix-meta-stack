# frozen_string_literal: true

require 'openssl'

# Chatwoot v4.7.0 API-inbox webhooks are not HMAC signed by the stock
# Webhooks::Trigger path. Conversation deletion is destructive, so the bridge
# extension signs this one callback explicitly instead of weakening the
# integration receiver.
#
# Do not serialize webhook URLs or HMAC tokens into ActiveJob arguments. The
# inbox is resolved again when the job actually runs so credential rotation
# between enqueue and delivery uses the current token and stale jobs cannot
# retain an old signing secret in the queue backend.
class MetaConversationDeleteWebhookJob < ApplicationJob
  queue_as :medium

  def perform(inbox_id, account_id, payload)
    inbox = Inbox.find_by(id: inbox_id, account_id: account_id)
    raise ArgumentError, 'conversation delete API inbox is missing' if inbox.blank? || inbox.channel_type != 'Channel::Api'

    channel = inbox.channel
    raise ArgumentError, 'conversation delete API channel is missing' if channel.blank?

    url = channel.webhook_url.to_s
    secret = channel.hmac_token.to_s
    raise ArgumentError, 'conversation delete webhook URL is missing' if url.blank?
    raise ArgumentError, 'conversation delete webhook HMAC token is missing' if secret.blank?

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
