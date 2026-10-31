import { ChannelEnvelope, ChannelMessage, type ChannelAttachment } from '../core/index.js';
import { telegramId } from './ids.js';

export type TelegramUpdateKind = 'message' | 'edited_message' | 'channel_post' |
  'edited_channel_post' | 'callback_query';

export interface TelegramReplyContext {
  readonly updateId: string;
  readonly kind: TelegramUpdateKind;
  readonly chatType: 'private' | 'group' | 'supergroup' | 'channel';
  readonly callbackQueryId?: string;
  readonly callbackData?: string;
  readonly inlineMessageId?: string;
}

export class TelegramNormalizationError extends Error {
  constructor() {
    super('Invalid Telegram message or identifier');
    this.name = 'TelegramNormalizationError';
  }
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
}

function attachment(value: unknown, kind: ChannelAttachment['kind']): ChannelAttachment | null {
  const file = record(value);
  if (!file) return null;
  const fileId = typeof file.file_id === 'string' && /^[A-Za-z0-9_-]+$/.test(file.file_id)
    ? file.file_id : undefined;
  if (!fileId) return null;
  return {
    kind, id: fileId,
    ...(typeof file.file_name === 'string' && file.file_name ? { name: file.file_name } : {}),
    ...(typeof file.mime_type === 'string' && file.mime_type ? { mimeType: file.mime_type } : {}),
    ...(typeof file.file_size === 'number' && Number.isSafeInteger(file.file_size) && file.file_size >= 0
      ? { sizeBytes: file.file_size } : {}),
  };
}

function attachments(message: Record<string, unknown>): ChannelAttachment[] {
  const result: ChannelAttachment[] = [];
  const photos = Array.isArray(message.photo) ? message.photo : [];
  const photo = attachment(photos.at(-1), 'image');
  if (photo) result.push(photo);
  for (const [field, kind] of [
    ['document', 'file'], ['audio', 'audio'], ['voice', 'audio'], ['video', 'video'],
    ['animation', 'video'], ['video_note', 'video'], ['sticker', 'image'],
  ] as const) {
    const item = attachment(message[field], kind);
    if (item) result.push(item);
  }
  return result;
}

/**
 * Telegram Bot API events only. Inline callbacks lack an authoritative chat
 * identity and are deliberately left for a dedicated transport handler.
 * Callback payloads remain in private reply context, outside model input.
 */
export function normalizeTelegramUpdate(
  update: unknown,
  options: { accountId: string },
): ChannelEnvelope<TelegramReplyContext> | null {
  const event = record(update);
  if (!event) throw new TelegramNormalizationError();
  const kind = (['message', 'edited_message', 'channel_post', 'edited_channel_post', 'callback_query'] as const)
    .find((field) => record(event[field]));
  if (!kind) return null;
  const callback = kind === 'callback_query' ? record(event.callback_query) : undefined;
  const message = callback ? record(callback.message) : record(event[kind]);
  if (!message) return null;
  const chat = record(message.chat);
  const chatType = chat?.type;
  if (!chat || (chatType !== 'private' && chatType !== 'group' && chatType !== 'supergroup' && chatType !== 'channel')) {
    throw new TelegramNormalizationError();
  }
  const text = callback ? '' : typeof message.text === 'string' ? message.text
    : typeof message.caption === 'string' ? message.caption : '';
  const files = callback ? [] : attachments(message);
  // Service events are not prompts. The host may register separate handlers.
  if (!callback && !text && !files.length) return null;
  try {
    const conversationId = telegramId(chat.id);
    const from = record(callback ? callback.from : message.from);
    const senderChat = callback ? undefined : record(message.sender_chat);
    const senderId = senderChat ? `chat:${telegramId(senderChat.id)}`
      : from ? telegramId(from.id)
      : chatType === 'channel' ? `chat:${conversationId}` : undefined;
    if (!senderId) throw new TypeError();
    const channelMessage = new ChannelMessage({
      platform: 'telegram', accountId: telegramId(options.accountId), conversationId,
      conversationType: chatType === 'private' ? 'direct' : chatType === 'channel' ? 'channel' : 'group',
      senderId, messageId: telegramId(message.message_id), text,
      ...(message.message_thread_id === undefined ? {} : { threadId: telegramId(message.message_thread_id) }),
      ...(record(message.reply_to_message)?.message_id === undefined ? {} : {
        replyToMessageId: telegramId(record(message.reply_to_message)!.message_id),
      }),
      attachments: files,
    });
    if (callback && (typeof callback.id !== 'string' || !callback.id)) throw new TypeError();
    return new ChannelEnvelope(channelMessage, Object.freeze({
      updateId: telegramId(event.update_id), kind, chatType,
      ...(callback ? { callbackQueryId: callback.id as string } : {}),
      ...(typeof callback?.data === 'string' ? { callbackData: callback.data } : {}),
      ...(typeof callback?.inline_message_id === 'string' ? { inlineMessageId: callback.inline_message_id } : {}),
    }));
  } catch { throw new TelegramNormalizationError(); }
}
