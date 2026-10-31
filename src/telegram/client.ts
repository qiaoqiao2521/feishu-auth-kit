import type { ChannelCapabilities, ChannelMessage } from '../core/index.js';
import { parseTelegramJson, stringifyTelegramJson, telegramId } from './ids.js';

type TelegramMethod = 'getMe' | 'getUpdates' | 'sendMessage' | 'editMessageText' |
  'answerCallbackQuery' | 'sendMessageDraft';
export type TelegramRawUpdate = Record<string, unknown>;

export class TelegramApiError extends Error {
  readonly deliveryStatus = 'failed' as const;
  constructor(
    readonly method: TelegramMethod,
    readonly errorCode: number | null,
    readonly httpStatus: number | null,
    readonly retryAfterSeconds?: number,
  ) {
    super(`Telegram ${method} rejected${errorCode === null ? '' : ` (code ${errorCode})`}`);
    this.name = 'TelegramApiError';
  }
}

/** An outgoing operation may have succeeded; callers must not automatically resend. */
export class TelegramDeliveryUnknownError extends Error {
  readonly deliveryStatus = 'unknown' as const;
  constructor(readonly method: TelegramMethod, readonly httpStatus: number | null = null) {
    super(`Telegram ${method} outcome unknown`);
    this.name = 'TelegramDeliveryUnknownError';
  }
}

export interface TelegramMessageReceipt {
  messageId: string;
  chatId: string;
  threadId?: string;
}

export interface TelegramSendMessageOptions {
  chatId: string;
  text: string;
  messageThreadId?: string;
  replyToMessageId?: string;
  signal?: AbortSignal;
}

export interface TelegramGetUpdatesOptions {
  offset?: string;
  limit?: number;
  timeout?: number;
  allowedUpdates?: readonly string[];
  signal?: AbortSignal;
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
}

function safeNonnegativeInteger(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}

function textWithin(value: string, max: number, empty = false): void {
  if (typeof value !== 'string' || (!empty && !value.length) || [...value].length > max) {
    throw new TypeError(`Telegram text must contain ${empty ? '0' : '1'}-${max} characters`);
  }
}

function integerParameter(id: string): bigint { return BigInt(telegramId(id)); }

export class TelegramBotClient {
  #token: string;
  #fetchFn: typeof fetch;
  #timeoutMs: number;
  #enableDrafts: boolean;

  constructor(token: string, options: {
    fetchFn?: typeof fetch;
    timeoutMs?: number;
    enableDrafts?: boolean;
  } = {}) {
    if (typeof token !== 'string' || !/^\d+:[A-Za-z0-9_-]+$/.test(token)) {
      throw new TypeError('A Telegram bot token is required');
    }
    this.#token = token;
    this.#fetchFn = options.fetchFn ?? globalThis.fetch;
    this.#timeoutMs = options.timeoutMs ?? 30_000;
    if (!Number.isSafeInteger(this.#timeoutMs) || this.#timeoutMs <= 0) {
      throw new TypeError('timeoutMs must be a positive safe integer');
    }
    this.#enableDrafts = options.enableDrafts === true;
  }

  get capabilities(): Readonly<ChannelCapabilities> {
    return Object.freeze({
      text: true, edit: true, threads: true, preview: this.#enableDrafts,
      attachments: false, reactions: false,
    });
  }

  async #request(method: TelegramMethod, body: Record<string, unknown>, options: {
    signal?: AbortSignal;
    timeoutMs?: number;
  } = {}): Promise<unknown> {
    const requestBody = stringifyTelegramJson(body);
    if (options.signal?.aborted) throw new TelegramApiError(method, null, null);
    let response: Response;
    try {
      response = await this.#fetchFn(`https://api.telegram.org/bot${this.#token}/${method}`, {
        method: 'POST',
        redirect: 'error',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: requestBody,
        signal: options.signal
          ? AbortSignal.any([options.signal, AbortSignal.timeout(options.timeoutMs ?? this.#timeoutMs)])
          : AbortSignal.timeout(options.timeoutMs ?? this.#timeoutMs),
      });
    } catch {
      // Fetch errors often include the credential-bearing URL. Do not retain a cause.
      throw new TelegramDeliveryUnknownError(method);
    }
    let payload: Record<string, unknown> | undefined;
    try { payload = record(parseTelegramJson(await response.text())); }
    catch { throw new TelegramDeliveryUnknownError(method, response.status); }
    if (payload?.ok === false) {
      const parameters = record(payload.parameters);
      throw new TelegramApiError(method,
        safeNonnegativeInteger(payload.error_code) ?? null,
        response.status,
        safeNonnegativeInteger(parameters?.retry_after));
    }
    if (!response.ok || payload?.ok !== true || !Object.hasOwn(payload, 'result')) {
      throw new TelegramDeliveryUnknownError(method, response.status);
    }
    return payload.result;
  }

  async getMe(options: { signal?: AbortSignal } = {}): Promise<{
    id: string; isBot: boolean; username?: string;
  }> {
    const result = record(await this.#request('getMe', {}, options));
    try {
      if (!result || typeof result.is_bot !== 'boolean') throw new TypeError();
      return { id: telegramId(result.id), isBot: result.is_bot,
        ...(typeof result.username === 'string' ? { username: result.username } : {}) };
    } catch { throw new TelegramDeliveryUnknownError('getMe'); }
  }

  async getUpdates(options: TelegramGetUpdatesOptions = {}): Promise<TelegramRawUpdate[]> {
    const timeout = options.timeout ?? 30;
    const limit = options.limit ?? 100;
    if (!Number.isSafeInteger(timeout) || timeout < 0 || timeout > 3600 ||
        !Number.isSafeInteger(limit) || limit < 1 || limit > 100) {
      throw new TypeError('Invalid Telegram polling limit or timeout');
    }
    const result = await this.#request('getUpdates', {
      ...(options.offset === undefined ? {} : { offset: integerParameter(options.offset) }),
      limit, timeout,
      ...(options.allowedUpdates === undefined ? {} : { allowed_updates: options.allowedUpdates }),
    }, { signal: options.signal, timeoutMs: Math.max(this.#timeoutMs, (timeout + 5) * 1000) });
    if (!Array.isArray(result) || !result.every((item) => record(item))) {
      throw new TelegramDeliveryUnknownError('getUpdates');
    }
    return result as TelegramRawUpdate[];
  }

  #receipt(method: TelegramMethod, value: unknown): TelegramMessageReceipt {
    try {
      const result = record(value);
      const chat = record(result?.chat);
      if (!result || !chat) throw new TypeError();
      return {
        messageId: telegramId(result.message_id), chatId: telegramId(chat.id),
        ...(result.message_thread_id === undefined ? {} : { threadId: telegramId(result.message_thread_id) }),
      };
    } catch { throw new TelegramDeliveryUnknownError(method); }
  }

  async sendMessage(options: TelegramSendMessageOptions): Promise<TelegramMessageReceipt> {
    textWithin(options.text, 4096);
    const chatId = telegramId(options.chatId);
    const result = await this.#request('sendMessage', {
      chat_id: chatId, text: options.text,
      ...(options.messageThreadId === undefined ? {} : { message_thread_id: integerParameter(options.messageThreadId) }),
      ...(options.replyToMessageId === undefined ? {} : {
        reply_parameters: { message_id: integerParameter(options.replyToMessageId) },
      }),
    }, { signal: options.signal });
    return this.#receipt('sendMessage', result);
  }

  reply(message: ChannelMessage, text: string, options: { signal?: AbortSignal } = {}): Promise<TelegramMessageReceipt> {
    if (message.platform !== 'telegram') throw new TypeError('A Telegram message is required');
    return this.sendMessage({ chatId: message.conversationId, text,
      replyToMessageId: message.messageId, messageThreadId: message.threadId, signal: options.signal });
  }

  async editMessageText(options: {
    chatId?: string; messageId?: string; inlineMessageId?: string; text: string; signal?: AbortSignal;
  }): Promise<TelegramMessageReceipt | true> {
    textWithin(options.text, 4096);
    const inline = options.inlineMessageId !== undefined;
    if (inline ? !options.inlineMessageId || options.chatId !== undefined || options.messageId !== undefined
      : options.chatId === undefined || options.messageId === undefined) {
      throw new TypeError('Choose inlineMessageId or chatId and messageId');
    }
    const result = await this.#request('editMessageText', {
      ...(inline ? { inline_message_id: options.inlineMessageId } : {
        chat_id: telegramId(options.chatId), message_id: integerParameter(options.messageId!),
      }), text: options.text,
    }, { signal: options.signal });
    if (inline && result === true) return true;
    return this.#receipt('editMessageText', result);
  }

  async answerCallbackQuery(options: {
    callbackQueryId: string; text?: string; showAlert?: boolean; signal?: AbortSignal;
  }): Promise<true> {
    if (typeof options.callbackQueryId !== 'string' || !options.callbackQueryId) {
      throw new TypeError('callbackQueryId is required');
    }
    if (options.text !== undefined) textWithin(options.text, 200, true);
    const result = await this.#request('answerCallbackQuery', {
      callback_query_id: options.callbackQueryId, text: options.text, show_alert: options.showAlert,
    }, { signal: options.signal });
    if (result !== true) throw new TelegramDeliveryUnknownError('answerCallbackQuery');
    return true;
  }

  /** Ephemeral preview only. Always call sendMessage separately to persist a final response. */
  async sendMessageDraft(options: {
    chatId: string; draftId: string; text: string; messageThreadId?: string; signal?: AbortSignal;
  }): Promise<{ phase: 'preview'; accepted: true }> {
    if (!this.#enableDrafts) throw new TypeError('Telegram draft capability is not enabled');
    textWithin(options.text, 4096, true);
    const chatId = integerParameter(options.chatId);
    const draftId = integerParameter(options.draftId);
    if (chatId <= 0n || draftId === 0n) throw new TypeError('Drafts require a private chat and nonzero draftId');
    const result = await this.#request('sendMessageDraft', {
      chat_id: chatId, draft_id: draftId, text: options.text,
      ...(options.messageThreadId === undefined ? {} : { message_thread_id: integerParameter(options.messageThreadId) }),
    }, { signal: options.signal });
    if (result !== true) throw new TelegramDeliveryUnknownError('sendMessageDraft');
    return { phase: 'preview', accepted: true };
  }
}
