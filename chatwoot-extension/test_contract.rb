# frozen_string_literal: true

require 'ostruct'

abort 'conversation.deleted event missing from Chatwoot' unless defined?(Events::Types::CONVERSATION_DELETED)
abort 'initializer module was not prepended' unless WebhookListener < MetaConversationDeleteWebhook

dispatcher_listeners = Rails.configuration.dispatcher.async_dispatcher.listeners
abort 'WebhookListener is not wired into Chatwoot async dispatcher' unless dispatcher_listeners.any? { |listener| listener.is_a?(WebhookListener) }

listener = WebhookListener.instance
channel = OpenStruct.new(
  webhook_url: 'https://integration.example.test/callback/test',
  secret: 'contract-secret'
)
inbox = OpenStruct.new(
  id: 2,
  account_id: 1,
  channel_type: 'Channel::Api',
  channel: channel
)

inbox_singleton = class << Inbox; self; end
webhook_singleton = class << WebhookJob; self; end
original_find_by = Inbox.method(:find_by)
original_perform_later = WebhookJob.method(:perform_later)
recorded = []

begin
  inbox_singleton.send(:define_method, :find_by) do |id:, account_id:|
    id.to_i == 2 && account_id.to_i == 1 ? inbox : nil
  end

  webhook_singleton.send(:define_method, :perform_later) do |*args, **kwargs|
    recorded << [args, kwargs]
  end

  event = OpenStruct.new(
    data: {
      conversation_data: {
        id: 77,
        account_id: 1,
        inbox_id: 2
      }
    }
  )

  listener.conversation_deleted(event)

  abort "expected exactly one API inbox delivery, got #{recorded.length}" unless recorded.length == 1
  args, kwargs = recorded.first
  abort 'wrong webhook URL' unless args[0] == channel.webhook_url
  abort 'wrong webhook type' unless args[2] == :api_inbox_webhook

  payload = args[1]
  abort 'wrong event name' unless payload[:event] == 'conversation_deleted'
  abort 'conversation id missing' unless payload[:conversation_id] == 77 && payload[:id] == 77
  abort 'account scope missing' unless payload.dig(:account, :id) == 1
  abort 'inbox scope missing' unless payload.dig(:inbox, :id) == 2
  abort 'API inbox secret was not delegated to stock delivery' unless kwargs[:secret] == channel.secret
  abort 'delivery id missing' if kwargs[:delivery_id].to_s.empty?

  recorded.clear
  inbox.channel_type = 'Channel::WebWidget'
  listener.conversation_deleted(event)
  abort 'non-API inbox deletion must not be forwarded' unless recorded.empty?

  recorded.clear
  inbox.channel_type = 'Channel::Api'
  missing_scope_event = OpenStruct.new(
    data: { conversation_data: { id: 77, account_id: 99, inbox_id: 2 } }
  )
  listener.conversation_deleted(missing_scope_event)
  abort 'missing scoped inbox must not be forwarded' unless recorded.empty?

  puts 'Chatwoot conversation deletion extension contract: OK'
ensure
  inbox_singleton.send(:define_method, :find_by, original_find_by)
  webhook_singleton.send(:define_method, :perform_later, original_perform_later)
end
