/** Public, transport-independent input. Credentials and raw events belong in an envelope. */
export type ConversationType = 'direct' | 'group' | 'channel';

export interface ChannelAttachment {
  readonly kind: 'image' | 'file' | 'audio' | 'video' | 'other';
  /** Opaque attachment reference; never a signed URL or credential. */
  readonly id?: string;
  readonly name?: string;
  readonly mimeType?: string;
  readonly sizeBytes?: number;
}

export interface ChannelMessageOptions {
  platform: string;
  accountId: string;
  conversationId: string;
  conversationType: ConversationType;
  senderId: string;
  messageId: string;
  threadId?: string;
  replyToMessageId?: string;
  text: string;
  attachments?: readonly ChannelAttachment[];
}

function requiredIdentity(value: string, field: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new TypeError(`${field} must be a nonempty string`);
  }
  return value;
}

function optionalIdentity(value: string | undefined, field: string): string | undefined {
  return value === undefined ? undefined : requiredIdentity(value, field);
}

export class ChannelMessage {
  readonly platform: string;
  readonly accountId: string;
  readonly conversationId: string;
  readonly conversationType: ConversationType;
  readonly senderId: string;
  readonly messageId: string;
  readonly threadId: string | undefined;
  readonly replyToMessageId: string | undefined;
  readonly text: string;
  readonly attachments: readonly ChannelAttachment[];

  constructor(options: ChannelMessageOptions) {
    this.platform = requiredIdentity(options.platform, 'platform');
    this.accountId = requiredIdentity(options.accountId, 'accountId');
    this.conversationId = requiredIdentity(options.conversationId, 'conversationId');
    if (!['direct', 'group', 'channel'].includes(options.conversationType)) {
      throw new TypeError('Unsupported conversationType');
    }
    this.conversationType = options.conversationType;
    this.senderId = requiredIdentity(options.senderId, 'senderId');
    this.messageId = requiredIdentity(options.messageId, 'messageId');
    this.threadId = optionalIdentity(options.threadId, 'threadId');
    this.replyToMessageId = optionalIdentity(options.replyToMessageId, 'replyToMessageId');
    if (typeof options.text !== 'string') throw new TypeError('text must be a string');
    this.text = options.text;
    this.attachments = Object.freeze((options.attachments ?? []).map((attachment) => {
      if (!['image', 'file', 'audio', 'video', 'other'].includes(attachment.kind)) {
        throw new TypeError('Unsupported attachment kind');
      }
      if (attachment.sizeBytes !== undefined &&
          (!Number.isSafeInteger(attachment.sizeBytes) || attachment.sizeBytes < 0)) {
        throw new TypeError('Attachment sizeBytes must be a nonnegative safe integer');
      }
      // Explicitly copy permitted fields. Transport metadata never enters the model.
      return Object.freeze({
        kind: attachment.kind,
        ...(attachment.id === undefined ? {} : { id: requiredIdentity(attachment.id, 'attachment.id') }),
        ...(attachment.name === undefined ? {} : { name: requiredIdentity(attachment.name, 'attachment.name') }),
        ...(attachment.mimeType === undefined ? {} : { mimeType: requiredIdentity(attachment.mimeType, 'attachment.mimeType') }),
        ...(attachment.sizeBytes === undefined ? {} : { sizeBytes: attachment.sizeBytes }),
      });
    }));
    Object.freeze(this);
  }

  promptText(): string { return this.text.trim(); }

  toDict(): Record<string, unknown> {
    return {
      schema: 'channel-kit.message.v1',
      platform: this.platform,
      account_id: this.accountId,
      conversation_id: this.conversationId,
      conversation_type: this.conversationType,
      sender_id: this.senderId,
      message_id: this.messageId,
      ...(this.threadId === undefined ? {} : { thread_id: this.threadId }),
      ...(this.replyToMessageId === undefined ? {} : { reply_to_message_id: this.replyToMessageId }),
      text: this.text,
      attachments: this.attachments.map((attachment) => ({
        kind: attachment.kind,
        ...(attachment.id === undefined ? {} : { id: attachment.id }),
        ...(attachment.name === undefined ? {} : { name: attachment.name }),
        ...(attachment.mimeType === undefined ? {} : { mime_type: attachment.mimeType }),
        ...(attachment.sizeBytes === undefined ? {} : { size_bytes: attachment.sizeBytes }),
      })),
    };
  }

  toJSON(): Record<string, unknown> { return this.toDict(); }
}

/** Transport-only reply context is deliberately excluded from every serialization path. */
export class ChannelEnvelope<TReplyContext> {
  readonly message: ChannelMessage;
  #replyContext: TReplyContext;

  constructor(message: ChannelMessage, replyContext: TReplyContext) {
    this.message = message;
    this.#replyContext = replyContext;
    Object.freeze(this);
  }

  getReplyContext(): TReplyContext { return this.#replyContext; }
  toDict(): Record<string, unknown> { return this.message.toDict(); }
  toJSON(): Record<string, unknown> { return this.toDict(); }
}

/** JSON tuples avoid delimiter collisions; sender isolation is enabled by default. */
export function buildSessionKey(
  message: ChannelMessage,
  options: { includeSender?: boolean } = {},
): string {
  return JSON.stringify([
    'channel-kit.session.v1', message.platform, message.accountId,
    message.conversationId, message.threadId ?? null,
    ...(options.includeSender === false ? [] : [message.senderId]),
  ]);
}
