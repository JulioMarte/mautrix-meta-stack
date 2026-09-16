# frozen_string_literal: true

require 'json'
require 'openssl'
require 'ostruct'


def assert(condition, message)
  raise "ASSERTION FAILED: #{message}" unless condition
end

assert(WebhookListener.ancestors.include?(MetaConversationDeleteWebhook),
       'WebhookListener is not patched with MetaConversationDeleteWebhook')
assert(MetaConversationDeleteWebhookJob < ApplicationJob,
       'MetaConversationDeleteWebhookJob is not an ActiveJob')

fake_channel = OpenStruct.new(
  webhook_url: 'http://integration:8080/webhooks/chatwoot/inbox',
  secret: 'contract-secret'
)
fake_inbox = OpenStruct.new(id: 2, channel_type: 'Channel::Api', channel: fake_channel)

inbox_singleton = Inbox.singleton_class
job_singleton = MetaConversationDeleteWebhookJob.singleton_class
inbox_singleton.alias_method :__meta_delete_original_find_by, :find_by
job_singleton.alias_method :__meta_delete_original_perform_later, :perform_later

captured_job = nil
begin
  inbox_singleton.define_method(:find_by) do |**kwargs|
    raise "unexpected Inbox.find_by args: #{kwargs.inspect}" unless kwargs == { id: 2, account_id: 1 }
    fake_inbox
  end
  job_singleton.define_method(:perform_later) do |*args|
    captured_job = args
    :queued
  end

  event = OpenStruct.new(data: { conversation_data: { id: 77, account_id: 1, inbox_id: 2 } })
  # Chatwoot intentionally makes WebhookListener.new private. Use send only in
  # this smoke so normal initialization still executes without changing the
  # production class visibility or bypassing its constructor.
  WebhookListener.send(:new).conversation_deleted(event)
ensure
  inbox_singleton.alias_method :find_by, :__meta_delete_original_find_by
  inbox_singleton.remove_method :__meta_delete_original_find_by
  job_singleton.alias_method :perform_later, :__meta_delete_original_perform_later
  job_singleton.remove_method :__meta_delete_original_perform_later
end

assert(captured_job, 'conversation_deleted did not enqueue the signed job')
url, payload, secret = captured_job
assert(url == fake_channel.webhook_url, 'wrong callback URL')
assert(secret == fake_channel.secret, 'wrong callback secret')
assert(payload[:event] == 'conversation_deleted', 'wrong event name')
assert(payload[:conversation_id] == 77 && payload[:id] == 77, 'wrong conversation id')
assert(payload.dig(:account, :id) == 1 && payload.dig(:inbox, :id) == 2, 'wrong scope payload')

request_singleton = RestClient::Request.singleton_class
request_singleton.alias_method :__meta_delete_original_execute, :execute
captured_request = nil
begin
  request_singleton.define_method(:execute) do |**kwargs|
    captured_request = kwargs
    OpenStruct.new(code: 200)
  end
  MetaConversationDeleteWebhookJob.new.perform(url, payload, secret)
ensure
  request_singleton.alias_method :execute, :__meta_delete_original_execute
  request_singleton.remove_method :__meta_delete_original_execute
end

assert(captured_request, 'signed job did not execute an HTTP request')
headers = captured_request[:headers]
body = captured_request[:payload]
timestamp = headers['X-Chatwoot-Timestamp']
signature = headers['X-Chatwoot-Signature']
expected = "sha256=#{OpenSSL::HMAC.hexdigest('SHA256', secret, "#{timestamp}.#{body}")}"

assert(captured_request[:method] == :post, 'callback is not POST')
assert(captured_request[:url] == url, 'callback URL changed inside the job')
assert(JSON.parse(body)['event'] == 'conversation_deleted', 'serialized payload is wrong')
assert(timestamp.to_i.positive?, 'timestamp header missing')
assert(signature == expected, 'HMAC signature does not match timestamp + raw body')

puts 'PASS: Chatwoot conversation deletion extension contract is loaded and HMAC signed'
