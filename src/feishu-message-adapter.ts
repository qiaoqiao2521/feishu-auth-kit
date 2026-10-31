import { ChannelEnvelope, ChannelMessage } from './core/message.js';
import { FeishuMessageContext } from './message-context.js';

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

function firstId(...values: unknown[]): string | undefined {
  return values.find((value): value is string => typeof value === 'string' && !!value.trim());
}

/** Adapt existing Feishu input while keeping raw events in transport-only context. */
export function adaptFeishuMessage(
  context: FeishuMessageContext,
  options: { accountId?: string } = {},
): ChannelEnvelope<FeishuMessageContext> {
  const accountId = options.accountId ?? context.appId;
  if (!accountId || !context.chatId || !context.messageId || !context.senderOpenId) {
    throw new TypeError('Feishu adaptation requires account, conversation, message and sender open IDs');
  }
  if (context.chatType !== 'p2p' && context.chatType !== 'group') {
    throw new TypeError('Unsupported Feishu conversation type');
  }
  // The legacy normalized context has no thread fields. Read only these public
  // routing IDs from its provider event, keeping the remainder private.
  const event = asRecord(context.raw.event ?? context.raw);
  const message = asRecord(event.message ?? context.raw.message);
  return new ChannelEnvelope(new ChannelMessage({
    platform: 'feishu',
    accountId,
    conversationId: context.chatId,
    conversationType: context.chatType === 'p2p' ? 'direct' : 'group',
    senderId: context.senderOpenId,
    messageId: context.messageId,
    threadId: firstId(message.thread_id, message.root_id),
    replyToMessageId: firstId(message.parent_id),
    text: context.promptText(),
  }), context);
}
