# frozen_string_literal: true

require 'json'
require 'openssl'
require 'ostruct'


def assert(condition, message)
  raise "ASSERTION FAILED: #{message}" unless condition
end

assert(DeleteObjectJob.ancestors.include?(MetaConversationDeleteObjectJobHook),
       'DeleteObjectJob is not patched with MetaConversationDeleteObjectJobHook')
assert(MetaConversationDeleteWebhookJob < ApplicationJob,
       'MetaConversationDeleteWebhookJob is not an ActiveJob')
assert(Channel::Api.column_names.include?('webhook_url'),
       'Chatwoot Channel::Api no longer exposes webhook_url')
assert(Channel::Api.column_names.include?('hmac_token'),
       'Chatwoot Channel::Api no longer exposes hmac_token')
assert(!defined?(Events::Types::CONVERSATION_DELETED),
       'Chatwoot v4.7.0 unexpectedly defines CONVERSATION_DELETED; re-review the hook boundary')

real_channel = Channel::Api.new(
  webhook_url: 'http://integration:8080/webhooks/chatwoot/inbox',
  hmac_token: 'enqueue-time-secret'
)
fake_inbox = OpenStruct.new(
  id: 2,
  account_id: 1,
  channel_type: 'Channel::Api',
  channel: real_channel
)

job_singleton = MetaConversationDeleteWebhookJob.singleton_class
job_singleton.alias_method :__meta_delete_original_perform_later, :perform_later
captured_job = nil
destroyed = false

conversation = Conversation.new
conversation.define_singleton_method(:id) { 77 }
conversation.define_singleton_method(:account_id) { 1 }
conversation.define_singleton_method(:inbox_id) { 2 }
conversation.define_singleton_method(:destroy!) do
  destroyed = true
  self
end

begin
  job_singleton.define_method(:perform_later) do |*args|
    captured_job = args
    :queued
  end
  DeleteObjectJob.new.perform(conversation)
ensure
  job_singleton.alias_method :perform_later, :__meta_delete_original_perform_later
  job_singleton.remove_method :__meta_delete_original_perform_later
end

assert(destroyed, 'DeleteObjectJob did not execute the real Conversation destroy path')
assert(captured_job, 'successful Conversation deletion did not enqueue the signed callback job')
inbox_id, payload = captured_job
assert(inbox_id == 2, 'job did not enqueue the API inbox id')
assert(captured_job.length == 2, 'job arguments unexpectedly contain additional data/secrets')
assert(!captured_job.flatten.map(&:to_s).include?('enqueue-time-secret'),
       'HMAC token leaked into serialized job arguments')
assert(payload[:event] == 'conversation_deleted', 'wrong event name')
assert(payload[:conversation_id] == 77 && payload[:id] == 77, 'wrong conversation id')
assert(payload.dig(:account, :id) == 1 && payload.dig(:inbox, :id) == 2, 'wrong scope payload')

# If Chatwoot fails to destroy the conversation, no remote delete may be emitted.
failed_conversation = Conversation.new
failed_conversation.define_singleton_method(:id) { 88 }
failed_conversation.define_singleton_method(:account_id) { 1 }
failed_conversation.define_singleton_method(:inbox_id) { 2 }
failed_conversation.define_singleton_method(:destroy!) { raise ActiveRecord::RecordNotDestroyed, 'simulated delete failure' }

job_singleton.alias_method :__meta_delete_original_perform_later_failure, :perform_later
unexpected_enqueue = false
begin
  job_singleton.define_method(:perform_later) do |*_args|
    unexpected_enqueue = true
    :queued
  end
  begin
    DeleteObjectJob.new.perform(failed_conversation)
    raise 'ASSERTION FAILED: failed Conversation delete did not raise'
  rescue ActiveRecord::RecordNotDestroyed
    # expected
  end
ensure
  job_singleton.alias_method :perform_later, :__meta_delete_original_perform_later_failure
  job_singleton.remove_method :__meta_delete_original_perform_later_failure
end
assert(!unexpected_enqueue, 'failed local Conversation deletion still enqueued a remote destructive callback')

# Non-conversation DeleteObjectJob executions must remain untouched.
plain_object = Object.new
plain_destroyed = false
plain_object.define_singleton_method(:destroy!) do
  plain_destroyed = true
  self
end
job_singleton.alias_method :__meta_delete_original_perform_later_nonconversation, :perform_later
nonconversation_enqueue = false
begin
  job_singleton.define_method(:perform_later) do |*_args|
    nonconversation_enqueue = true
    :queued
  end
  DeleteObjectJob.new.perform(plain_object)
ensure
  job_singleton.alias_method :perform_later, :__meta_delete_original_perform_later_nonconversation
  job_singleton.remove_method :__meta_delete_original_perform_later_nonconversation
end
assert(plain_destroyed, 'DeleteObjectJob behavior changed for non-conversation objects')
assert(!nonconversation_enqueue, 'non-conversation deletion enqueued a conversation callback')

# Delivery still resolves the current inbox and HMAC token at execution time.
real_channel.hmac_token = 'rotated-execution-secret'
inbox_singleton = Inbox.singleton_class
request_singleton = RestClient::Request.singleton_class
inbox_singleton.alias_method :__meta_delete_original_find_by_job, :find_by
request_singleton.alias_method :__meta_delete_original_execute, :execute
captured_request = nil
begin
  inbox_singleton.define_method(:find_by) do |**kwargs|
    raise "unexpected job Inbox.find_by args: #{kwargs.inspect}" unless kwargs == { id: 2 }
    fake_inbox
  end
  request_singleton.define_method(:execute) do |**kwargs|
    captured_request = kwargs
    OpenStruct.new(code: 200)
  end
  MetaConversationDeleteWebhookJob.new.perform(inbox_id, payload)
ensure
  inbox_singleton.alias_method :find_by, :__meta_delete_original_find_by_job
  inbox_singleton.remove_method :__meta_delete_original_find_by_job
  request_singleton.alias_method :execute, :__meta_delete_original_execute
  request_singleton.remove_method :__meta_delete_original_execute
end

assert(captured_request, 'signed job did not execute an HTTP request')
headers = captured_request[:headers]
body = captured_request[:payload]
timestamp = headers['X-Chatwoot-Timestamp']
signature = headers['X-Chatwoot-Signature']
expected = "sha256=#{OpenSSL::HMAC.hexdigest('SHA256', 'rotated-execution-secret', "#{timestamp}.#{body}")}"
stale = "sha256=#{OpenSSL::HMAC.hexdigest('SHA256', 'enqueue-time-secret', "#{timestamp}.#{body}")}"

assert(captured_request[:method] == :post, 'callback is not POST')
assert(captured_request[:url] == real_channel.webhook_url, 'callback URL changed inside the job')
assert(JSON.parse(body)['event'] == 'conversation_deleted', 'serialized payload is wrong')
assert(timestamp.to_i.positive?, 'timestamp header missing')
assert(signature == expected, 'HMAC signature did not use current execution-time token')
assert(signature != stale, 'HMAC signature incorrectly used stale enqueue-time token')

# Stale scope, missing inbox, or malformed delayed jobs must fail closed.
def execute_without_http(inbox_singleton, request_singleton, inbox_result, inbox_id, payload)
  captured = false
  inbox_singleton.alias_method :__meta_delete_original_find_by_guard, :find_by
  request_singleton.alias_method :__meta_delete_original_execute_guard, :execute
  begin
    inbox_singleton.define_method(:find_by) { |**_kwargs| inbox_result }
    request_singleton.define_method(:execute) do |**_kwargs|
      captured = true
      OpenStruct.new(code: 200)
    end
    MetaConversationDeleteWebhookJob.new.perform(inbox_id, payload)
  ensure
    inbox_singleton.alias_method :find_by, :__meta_delete_original_find_by_guard
    inbox_singleton.remove_method :__meta_delete_original_find_by_guard
    request_singleton.alias_method :execute, :__meta_delete_original_execute_guard
    request_singleton.remove_method :__meta_delete_original_execute_guard
  end
  captured
end

assert(!execute_without_http(inbox_singleton, request_singleton, nil, inbox_id, payload),
       'deleted inbox still caused an HTTP callback')
wrong_scope_inbox = OpenStruct.new(id: 2, account_id: 999, channel_type: 'Channel::Api', channel: real_channel)
assert(!execute_without_http(inbox_singleton, request_singleton, wrong_scope_inbox, inbox_id, payload),
       'scope-changed inbox still caused an HTTP callback')
assert(!execute_without_http(inbox_singleton, request_singleton, fake_inbox, inbox_id, nil),
       'malformed nil payload still caused an HTTP callback')
assert(!execute_without_http(inbox_singleton, request_singleton, fake_inbox, inbox_id, 'legacy-corrupt-payload'),
       'malformed string payload still caused an HTTP callback')

puts 'PASS: Chatwoot v4.7 deletion contract hooks real DeleteObjectJob, emits only after successful Conversation destroy, and signs with execution-time hmac_token'
