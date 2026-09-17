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
assert(Channel::Api.column_names.include?('webhook_url'),
       'Chatwoot Channel::Api no longer exposes webhook_url')
assert(Channel::Api.column_names.include?('hmac_token'),
       'Chatwoot Channel::Api no longer exposes hmac_token')

real_channel = Channel::Api.new(
  webhook_url: 'http://integration:8080/webhooks/chatwoot/inbox',
  hmac_token: 'contract-secret-before-rotation'
)
fake_inbox = OpenStruct.new(id: 2, account_id: 1, channel_type: 'Channel::Api', channel: real_channel)

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
  WebhookListener.send(:new).conversation_deleted(event)
ensure
  job_singleton.alias_method :perform_later, :__meta_delete_original_perform_later
  job_singleton.remove_method :__meta_delete_original_perform_later
end

assert(captured_job, 'conversation_deleted did not enqueue the signed job')
inbox_id, account_id, payload = captured_job
assert(inbox_id == 2 && account_id == 1, 'job did not persist stable inbox/account identifiers')
assert(payload[:event] == 'conversation_deleted', 'wrong event name')
assert(payload[:conversation_id] == 77 && payload[:id] == 77, 'wrong conversation id')
assert(payload.dig(:account, :id) == 1 && payload.dig(:inbox, :id) == 2, 'wrong scope payload')
serialized_job_args = captured_job.inspect
assert(!serialized_job_args.include?('contract-secret-before-rotation'),
       'HMAC token leaked into serialized ActiveJob arguments')
assert(!serialized_job_args.include?(real_channel.webhook_url),
       'callback URL leaked into serialized ActiveJob arguments')

# Rotate both endpoint and HMAC token after enqueue. The queued job must resolve
# the live Channel::Api state at execution time rather than replaying stale data.
real_channel.webhook_url = 'http://integration:8080/webhooks/chatwoot/inbox-rotated'
real_channel.hmac_token = 'contract-secret-after-rotation'

request_singleton = RestClient::Request.singleton_class
request_singleton.alias_method :__meta_delete_original_execute, :execute
captured_request = nil
begin
  request_singleton.define_method(:execute) do |**kwargs|
    captured_request = kwargs
    OpenStruct.new(code: 200)
  end
  MetaConversationDeleteWebhookJob.new.perform(inbox_id, account_id, payload)
ensure
  request_singleton.alias_method :execute, :__meta_delete_original_execute
  request_singleton.remove_method :__meta_delete_original_execute
  inbox_singleton.alias_method :find_by, :__meta_delete_original_find_by
  inbox_singleton.remove_method :__meta_delete_original_find_by
end

assert(captured_request, 'signed job did not execute an HTTP request')
headers = captured_request[:headers]
body = captured_request[:payload]
timestamp = headers['X-Chatwoot-Timestamp']
signature = headers['X-Chatwoot-Signature']
expected = "sha256=#{OpenSSL::HMAC.hexdigest('SHA256', real_channel.hmac_token, "#{timestamp}.#{body}")}"
old_signature = "sha256=#{OpenSSL::HMAC.hexdigest('SHA256', 'contract-secret-before-rotation', "#{timestamp}.#{body}")}"

assert(captured_request[:method] == :post, 'callback is not POST')
assert(captured_request[:url] == real_channel.webhook_url, 'job did not use rotated callback URL')
assert(JSON.parse(body)['event'] == 'conversation_deleted', 'serialized payload is wrong')
assert(timestamp.to_i.positive?, 'timestamp header missing')
assert(signature == expected, 'HMAC signature does not use current Channel::Api.hmac_token')
assert(signature != old_signature, 'HMAC signature incorrectly used the enqueue-time token')

puts 'PASS: deletion callback resolves current Channel::Api endpoint/HMAC at execution and keeps secrets out of job args'
