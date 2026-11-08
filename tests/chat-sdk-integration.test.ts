import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
// Exercise the example's thin policy/model seam around real released SDK objects.
import { createTelegramPilot } from '../examples/chat-sdk/telegram.mjs';

const bots: ReturnType<typeof createTelegramPilot>[] = [];
const calls: Array<{ method: string; body: Record<string, any> }> = [];
beforeEach(() => {
  calls.length = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string | URL, init?: RequestInit) => {
    const method = String(url).split('/').at(-1)!;
    const body = JSON.parse(String(init?.body ?? '{}'));
    calls.push({ method, body });
    let result;
    if (method === 'getMe') result = { id: 123, is_bot: true, username: 'testbot', first_name: 'Test' };
    else if (method === 'sendChatAction') result = true;
    else if (method === 'sendMessage') result = { message_id: 500, date: 1, text: body.text,
      chat: { id: Number(body.chat_id), type: 'private' },
      from: { id: 123, is_bot: true, first_name: 'Test' } };
    else throw new Error('No real provider calls are permitted in this test');
    return Response.json({ ok: true, result });
  }));
});
afterEach(async () => {
  await Promise.all(bots.splice(0).map((bot) => bot.shutdown()));
  vi.unstubAllGlobals();
});
function pilot(options = {}) {
  const bot = createTelegramPilot({ accountId: '123', botToken: '123:FAKE_TEST_TOKEN',
    secretToken: 'fake-secret', userName: 'testbot', grants: [{ userId: '42', chatId: '42' }], ...options });
  bots.push(bot); return bot;
}
async function receive(bot: ReturnType<typeof createTelegramPilot>, options: {
  id?: number; senderId?: number; chatId?: number; secret?: string | null; threadId?: number;
} = {}) {
  const tasks: Promise<unknown>[] = [];
  const secret = options.secret === undefined ? 'fake-secret' : options.secret;
  const response = await bot.webhooks.telegram(new Request('https://example.invalid/webhook', {
    method: 'POST', headers: { 'content-type': 'application/json',
      ...(secret === null ? {} : { 'x-telegram-bot-api-secret-token': secret }) },
    body: JSON.stringify({ update_id: options.id ?? 1, message: {
      message_id: 6 + (options.id ?? 1), date: 1, text: 'hello',
      from: { id: options.senderId ?? 42, is_bot: false, first_name: 'Owner' },
      chat: { id: options.chatId ?? 42, type: options.threadId ? 'supergroup' : 'private' },
      ...(options.threadId ? { message_thread_id: options.threadId } : {}),
      private_transport_value: 'DO_NOT_SERIALIZE',
    } }),
  }), { waitUntil: (promise) => tasks.push(promise) });
  await Promise.all(tasks); return response;
}

describe('reuse-first Telegram composition pilot', () => {
  it('delegates verified ingress and reply to the SDK while selecting only model input fields', async () => {
    const respond = vi.fn(async () => 'Selected response'); const bot = pilot({ respond });
    expect((await receive(bot)).status).toBe(200);
    expect(respond).toHaveBeenCalledOnce();
    expect(respond.mock.calls[0][0]).toEqual({ accountId: '123', threadId: 'telegram:42',
      messageId: '42:7', senderId: '42', text: 'hello' });
    expect(JSON.stringify(respond.mock.calls[0])).not.toContain('DO_NOT_SERIALIZE');
    const sends = calls.filter((call) => call.method === 'sendMessage');
    expect(sends).toHaveLength(1);
    expect(sends[0].body).toMatchObject({ chat_id: '42', text: 'Selected response' });
  });
  it.each([null, 'wrong-secret'])('rejects a webhook with secret %s before executing application code', async (secret) => {
    const respond = vi.fn(async () => 'never'); const bot = pilot({ respond });
    expect((await receive(bot, { secret })).status).toBeGreaterThanOrEqual(400);
    expect(respond).not.toHaveBeenCalled();
    expect(calls.some((call) => call.method === 'sendMessage')).toBe(false);
  });
  it.each([{ senderId: 99 }, { chatId: 99 }])('denies a mismatched scoped grant %j', async (scope) => {
    const respond = vi.fn(async () => 'never'); const bot = pilot({ respond });
    await receive(bot, scope); expect(respond).not.toHaveBeenCalled();
    expect(calls.some((call) => call.method === 'sendMessage')).toBe(false);
  });
  it('lets the existing adapter perform duplicate update suppression', async () => {
    const respond = vi.fn(async () => 'one response'); const bot = pilot({ respond });
    await receive(bot); await receive(bot);
    expect(respond).toHaveBeenCalledOnce();
    expect(calls.filter((call) => call.method === 'sendMessage')).toHaveLength(1);
  });
  it('applies topic scope before calling the response handler', async () => {
    const respond = vi.fn(async () => 'topic answer'); const bot = pilot({ respond,
      grants: [{ userId: '42', chatId: '-10042', topicId: 9 }] });
    await receive(bot, { id: 1, chatId: -10042, threadId: 10 });
    expect(respond).not.toHaveBeenCalled();
    await receive(bot, { id: 2, chatId: -10042, threadId: 9 });
    expect(respond).toHaveBeenCalledOnce();
    expect(calls.find((call) => call.method === 'sendMessage')?.body)
      .toMatchObject({ chat_id: '-10042', message_thread_id: 9 });
  });
  it('requires explicit nonempty grants and a webhook secret at construction', () => {
    expect(() => pilot({ grants: [] })).toThrow('grants');
    expect(() => pilot({ secretToken: '' })).toThrow('secret');
    expect(() => pilot({ accountId: '456' })).toThrow('account');
    expect(calls).toEqual([]);
  });
});
