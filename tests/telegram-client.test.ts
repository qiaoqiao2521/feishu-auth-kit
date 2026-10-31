import { describe, expect, it, vi } from 'vitest';
import {
  TelegramApiError, TelegramBotClient, TelegramDeliveryUnknownError,
} from '../src/telegram/client.js';
import { normalizeTelegramUpdate } from '../src/telegram/normalizer.js';

const token = '123:FAKE_TOKEN';
function response(result: unknown) { return new Response(JSON.stringify({ ok: true, result })); }
function messageResponse() { return response({ message_id: 42, chat: { id: -100123 }, message_thread_id: 7 }); }

describe('TelegramBotClient (mock network only)', () => {
  it('advertises text, edit, and topics with previews only when explicitly enabled', () => {
    const client = new TelegramBotClient(token);
    expect(client.capabilities).toEqual({ text: true, edit: true, threads: true,
      preview: false, attachments: false, reactions: false });
    expect(Object.isFrozen(client.capabilities)).toBe(true);
    expect(new TelegramBotClient(token, { enableDrafts: true }).capabilities.preview).toBe(true);
  });

  it('keeps credentials private and verifies getMe without exposing raw user fields', async () => {
    const fetchFn = vi.fn(async () => response({ id: 123, is_bot: true, username: 'example_bot', private: 'ignored' }));
    const client = new TelegramBotClient(token, { fetchFn });
    expect(JSON.stringify(client)).toBe('{}');
    expect(Object.getOwnPropertyNames(client)).toEqual([]);
    await expect(client.getMe()).resolves.toEqual({ id: '123', isBot: true, username: 'example_bot' });
    expect(fetchFn.mock.calls[0][0]).toBe(`https://api.telegram.org/bot${token}/getMe`);
    expect(fetchFn.mock.calls[0][1]?.redirect).toBe('error');
  });

  it('uses explicit offset, update filters, and long-poll options', async () => {
    const fetchFn = vi.fn(async () => response([{ update_id: 9 }]));
    const client = new TelegramBotClient(token, { fetchFn });
    await expect(client.getUpdates({ offset: '9', timeout: 60, limit: 20, allowedUpdates: ['message'] }))
      .resolves.toEqual([{ update_id: 9 }]);
    const body = JSON.parse(String(fetchFn.mock.calls[0][1]?.body));
    expect(body).toEqual({ offset: 9, timeout: 60, limit: 20, allowed_updates: ['message'] });
  });

  it('preserves integers beyond JS precision and escaped text in incoming JSON', async () => {
    const text = '{"ok":true,"result":[{"update_id":9007199254740993,"message":{"message_id":9007199254740995,"chat":{"id":-9007199254740997,"type":"supergroup"},"from":{"id":9007199254740999},"text":"escaped \\\"9007199254740991\\\""}}]}';
    const client = new TelegramBotClient(token, { fetchFn: vi.fn(async () => new Response(text)) });
    const updates = await client.getUpdates();
    const normalized = normalizeTelegramUpdate(updates[0], { accountId: '123' })!;
    expect(normalized.getReplyContext().updateId).toBe('9007199254740993');
    expect(normalized.message.messageId).toBe('9007199254740995');
    expect(normalized.message.conversationId).toBe('-9007199254740997');
    expect(normalized.message.senderId).toBe('9007199254740999');
    expect(normalized.message.text).toBe('escaped "9007199254740991"');
  });

  it('sends exact thread and reply integers and returns a minimal receipt', async () => {
    const fetchFn = vi.fn(async () => messageResponse());
    const client = new TelegramBotClient(token, { fetchFn });
    await expect(client.sendMessage({ chatId: '-100123', text: 'hello',
      messageThreadId: '9007199254740993', replyToMessageId: '9007199254740995' }))
      .resolves.toEqual({ messageId: '42', chatId: '-100123', threadId: '7' });
    const body = String(fetchFn.mock.calls[0][1]?.body);
    expect(body).toContain('"message_thread_id":9007199254740993');
    expect(body).toContain('"message_id":9007199254740995');
    expect(body).toContain('"chat_id":"-100123"');
  });

  it('replies in the source topic to the source message', async () => {
    const envelope = normalizeTelegramUpdate({ update_id: 1, message: {
      message_id: 2, message_thread_id: 3, chat: { id: -4, type: 'supergroup' }, from: { id: 5 }, text: 'hi',
    } }, { accountId: '123' })!;
    const fetchFn = vi.fn(async () => messageResponse());
    await new TelegramBotClient(token, { fetchFn }).reply(envelope.message, 'reply');
    expect(JSON.parse(String(fetchFn.mock.calls[0][1]?.body))).toMatchObject({
      chat_id: '-4', message_thread_id: 3, reply_parameters: { message_id: 2 }, text: 'reply',
    });
  });

  it('edits chat or inline messages and answers callback queries', async () => {
    const fetchFn = vi.fn()
      .mockResolvedValueOnce(messageResponse())
      .mockResolvedValueOnce(response(true))
      .mockResolvedValueOnce(response(true));
    const client = new TelegramBotClient(token, { fetchFn });
    await client.editMessageText({ chatId: '-100123', messageId: '42', text: 'edited' });
    await expect(client.editMessageText({ inlineMessageId: 'opaque-inline-id', text: 'edited' })).resolves.toBe(true);
    await expect(client.answerCallbackQuery({ callbackQueryId: 'opaque-query-id', text: 'done' })).resolves.toBe(true);
    expect(JSON.parse(String(fetchFn.mock.calls[1][1]?.body))).toEqual({ inline_message_id: 'opaque-inline-id', text: 'edited' });
    expect(JSON.parse(String(fetchFn.mock.calls[2][1]?.body))).toEqual({ callback_query_id: 'opaque-query-id', text: 'done' });
  });

  it('treats structured API rejection as failed and does not retry or leak server descriptions', async () => {
    const fetchFn = vi.fn(async () => new Response(JSON.stringify({ ok: false, error_code: 429,
      description: `secret ${token} https://api.telegram.org/bot${token}/sendMessage`,
      parameters: { retry_after: 5 } }), { status: 429 }));
    const client = new TelegramBotClient(token, { fetchFn });
    const error = await client.sendMessage({ chatId: '1', text: 'hello' }).catch((err) => err);
    expect(error).toBeInstanceOf(TelegramApiError);
    expect(error).toMatchObject({ deliveryStatus: 'failed', errorCode: 429, httpStatus: 429, retryAfterSeconds: 5 });
    expect(String(error)).not.toContain(token);
    expect(JSON.stringify(error)).not.toContain(token);
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it.each(['network', 'html', 'malformed', 'missing-result', 'bad-receipt'])('retains unknown delivery after %s failure', async (kind) => {
    const fetchFn = vi.fn(async () => {
      if (kind === 'network') throw new Error(`failed https://api.telegram.org/bot${token}/sendMessage`);
      if (kind === 'html') return new Response(`proxy ${token}`, { status: 502 });
      if (kind === 'malformed') return new Response('[]');
      if (kind === 'missing-result') return new Response('{"ok":true}');
      return response({ text: 'posted but receipt missing' });
    });
    const error = await new TelegramBotClient(token, { fetchFn })
      .sendMessage({ chatId: '1', text: 'hello' }).catch((err) => err);
    expect(error).toBeInstanceOf(TelegramDeliveryUnknownError);
    expect(error.deliveryStatus).toBe('unknown');
    expect(error.cause).toBeUndefined();
    expect(String(error)).not.toContain(token);
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it('requires explicit draft capability and keeps draft success separate from final delivery', async () => {
    const fetchFn = vi.fn(async () => response(true));
    await expect(new TelegramBotClient(token, { fetchFn }).sendMessageDraft({ chatId: '1', draftId: '5', text: '' }))
      .rejects.toThrow('capability');
    expect(fetchFn).not.toHaveBeenCalled();
    const client = new TelegramBotClient(token, { fetchFn, enableDrafts: true });
    await expect(client.sendMessageDraft({ chatId: '1', draftId: '5', text: '' }))
      .resolves.toEqual({ phase: 'preview', accepted: true });
    expect(String(fetchFn.mock.calls[0][0])).toMatch(/\/sendMessageDraft$/);
    expect(fetchFn).toHaveBeenCalledTimes(1);
    await expect(client.sendMessageDraft({ chatId: '-100', draftId: '5', text: 'x' })).rejects.toThrow('private chat');
  });

  it('validates local parameters before any network dispatch', async () => {
    const fetchFn = vi.fn();
    const client = new TelegramBotClient(token, { fetchFn });
    await expect(client.sendMessage({ chatId: '1', text: '' })).rejects.toThrow(TypeError);
    await expect(client.getUpdates({ limit: 101 })).rejects.toThrow(TypeError);
    await expect(client.editMessageText({ chatId: '1', inlineMessageId: 'x', messageId: '2', text: 'x' }))
      .rejects.toThrow(TypeError);
    const controller = new AbortController(); controller.abort();
    await expect(client.sendMessage({ chatId: '1', text: 'hello', signal: controller.signal }))
      .rejects.toMatchObject({ deliveryStatus: 'failed' });
    expect(fetchFn).not.toHaveBeenCalled();
  });
});
