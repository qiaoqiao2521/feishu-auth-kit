import { afterEach, describe, expect, it, vi } from 'vitest';
import { Actions, Button, Card, Chat, ConsoleLogger } from 'chat';
import { createTelegramAdapter } from '@chat-adapter/telegram';
import { createMemoryState } from '@chat-adapter/state-memory';

// Exercise the released ecosystem adapter. All outgoing traffic is replaced
// before constructing it; an unrecognized method fails instead of using a wire.
afterEach(() => vi.unstubAllGlobals());

const rawMessage = (overrides = {}) => ({
  message_id: 7, date: 1, text: 'hello',
  from: { id: 42, is_bot: false, first_name: 'Owner' },
  chat: { id: 42, type: 'private' as const, first_name: 'Owner' },
  ...overrides,
});

function setupFetch(respond?: (method: string, body: Record<string, any>) => unknown) {
  const calls: Array<{ method: string; body: Record<string, any> }> = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL, init?: RequestInit) => {
    const method = String(input).split('/').at(-1)!;
    const body = JSON.parse(String(init?.body ?? '{}'));
    calls.push({ method, body });
    let result = respond?.(method, body);
    if (result === undefined) {
      if (method === 'getMe') result = { id: 123, is_bot: true, first_name: 'Test', username: 'testbot' };
      else if (method === 'sendChatAction' || method === 'answerCallbackQuery') result = true;
      else throw new Error(`Unmocked Telegram method: ${method}`);
    }
    return new Response(JSON.stringify({ ok: true, result }));
  }));
  return calls;
}

function adapter(options = {}) {
  return createTelegramAdapter({
    botToken: '123:FAKE_TEST_TOKEN', secretToken: 'fake-webhook-secret',
    mode: 'webhook', userName: 'testbot', logger: new ConsoleLogger('silent'),
    ...options,
  });
}

function sentMessage(body: Record<string, any>) {
  return rawMessage({ message_id: 500, chat: { id: Number(body.chat_id), type: 'private' },
    text: body.text ?? body.rich_message?.markdown ?? '',
    from: { id: 123, is_bot: true, first_name: 'Test' } });
}

describe('released Chat SDK Telegram adapter', () => {
  it('normalizes topics, captions, replies, and lazy attachment references through parseMessage', () => {
    const calls = setupFetch();
    const telegram = adapter();
    const message = telegram.parseMessage(rawMessage({
      chat: { id: -10042, type: 'supergroup' }, message_thread_id: 55,
      text: undefined, caption: 'photo caption',
      photo: [{ file_id: 'opaque-file', file_unique_id: 'stable-file', width: 80, height: 60 }],
      reply_to_message: rawMessage({ message_id: 6, text: 'earlier' }),
    }));
    expect(message.id).toBe('-10042:7');
    expect(message.threadId).toBe('telegram:-10042:55');
    expect(message.author.userId).toBe('42');
    expect(message.text).toBe('photo caption');
    expect(message.replyTo?.text).toBe('earlier');
    expect(message.attachments?.[0]).toMatchObject({ type: 'image', mimeType: 'image/jpeg',
      fetchMetadata: { fileId: 'opaque-file', fileUniqueId: 'stable-file' } });
    expect(typeof message.attachments?.[0]?.fetchData).toBe('function');
    expect(calls).toEqual([]);
  });

  it('renders SDK buttons and routes the resulting callback through the verified webhook', async () => {
    const calls = setupFetch((method, body) => method === 'sendMessage' ? sentMessage(body) : undefined);
    const telegram = adapter();
    const bot = new Chat({ userName: 'testbot', adapters: { telegram }, state: createMemoryState(), logger: 'silent' });
    const action = vi.fn();
    bot.onAction('approve', action);
    await bot.initialize();
    await telegram.postMessage('telegram:42', Card({ title: 'Decision', children: [
      Actions([Button({ id: 'approve', label: 'Approve', value: 'yes' })]),
    ] }));
    const send = calls.find(({ method }) => method === 'sendMessage')!;
    const callbackData = send.body.reply_markup.inline_keyboard[0][0].callback_data;
    const tasks: Promise<unknown>[] = [];
    const response = await bot.webhooks.telegram(new Request('https://example.invalid/telegram', {
      method: 'POST', headers: { 'content-type': 'application/json',
        'x-telegram-bot-api-secret-token': 'fake-webhook-secret' },
      body: JSON.stringify({ update_id: 1, callback_query: {
        id: 'fake-query', from: rawMessage().from, data: callbackData,
        chat_instance: 'fake-chat-instance', message: sentMessage({ chat_id: 42, text: 'Decision' }),
      } }),
    }), { waitUntil: (task) => tasks.push(task) });
    await Promise.all(tasks);
    expect(response.status).toBe(200);
    expect(action).toHaveBeenCalledOnce();
    expect(action.mock.calls[0][0]).toMatchObject({ actionId: 'approve', value: 'yes',
      threadId: 'telegram:42', user: { userId: '42' } });
    expect(calls.filter(({ method }) => method === 'answerCallbackQuery')).toHaveLength(1);
  });

  it('finishes native draft streaming with a separate persisted final message', async () => {
    const calls = setupFetch((method, body) => {
      if (method === 'sendRichMessageDraft') return true;
      if (method === 'sendRichMessage') return sentMessage(body);
    });
    const telegram = adapter({ nativeStreaming: true });
    async function* chunks() { yield 'Hello'; yield ' world'; }
    const result = await telegram.stream('telegram:42', chunks(), { updateIntervalMs: 0 });
    expect(calls.filter(({ method }) => method === 'sendRichMessageDraft').length).toBeGreaterThan(0);
    expect(calls.filter(({ method }) => method === 'sendRichMessage')).toHaveLength(1);
    expect(calls.at(-1)?.method).toBe('sendRichMessage');
    expect(result?.id).toBe('42:500');
    expect(result?.raw.text).toBe('Hello world');
  });

  it('does not report final delivery or resend when final send fails after successful previews', async () => {
    const calls = setupFetch((method) => {
      if (method === 'sendRichMessageDraft') return true;
      if (method === 'sendRichMessage') throw new Error('simulated lost acknowledgement');
    });
    const telegram = adapter({ nativeStreaming: true });
    async function* chunks() { yield 'Hello'; }
    await expect(telegram.stream('telegram:42', chunks(), { updateIntervalMs: 0 }))
      .rejects.toMatchObject({ name: 'NetworkError' });
    expect(calls.filter(({ method }) => method === 'sendRichMessageDraft').length).toBeGreaterThan(0);
    expect(calls.filter(({ method }) => method === 'sendRichMessage')).toHaveLength(1);
    expect(calls.some(({ method }) => method === 'sendMessage')).toBe(false);
  });

  it('does not automatically resend an ordinary post after an ambiguous network failure', async () => {
    const calls = setupFetch((method) => {
      if (method === 'sendMessage') throw new Error('simulated lost acknowledgement');
    });
    await expect(adapter().postMessage('telegram:42', 'hello'))
      .rejects.toMatchObject({ name: 'NetworkError' });
    expect(calls.map(({ method }) => method)).toEqual(['sendMessage']);
  });
});
