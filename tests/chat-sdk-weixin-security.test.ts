import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Chat } from 'chat';
import { createMemoryState } from '@chat-adapter/state-memory';
import { WeixinAdapter, startWeixinLogin } from 'chat-adapter-weixin';

// Characterization of the exact pinned 0.1.4 release, not a security pass.
// Provider HTTP is replaced; no real QR, credentials, polling or sending occurs.
type Call = { path: string; body: any; authorization: string | null };
const bots: Chat[] = [];
const calls: Call[] = [];
const pending = new Map<string, any[]>();
const temporary: string[] = [];
let offlineFetch: typeof fetch;

beforeEach(() => {
  calls.length = 0;
  pending.clear();
  offlineFetch = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    if (url.origin !== 'https://offline.example.invalid') throw new Error('Unexpected offline URL');
    const authorization = new Headers(init?.headers).get('authorization');
    const body = JSON.parse(String(init?.body ?? '{}'));
    calls.push({ path: url.pathname, body, authorization });
    if (url.pathname === '/ilink/bot/getupdates') {
      const msgs = pending.get(authorization ?? '') ?? [];
      pending.delete(authorization ?? '');
      return Response.json({ ret: 0, msgs });
    }
    if (url.pathname === '/ilink/bot/sendmessage' || url.pathname === '/ilink/bot/sendtyping') {
      return Response.json({ ret: 0 });
    }
    if (url.pathname === '/ilink/bot/getconfig') return Response.json({ ret: 0, typing_ticket: 'synthetic-ticket' });
    throw new Error('Unexpected offline endpoint');
  }) as typeof fetch;
  vi.stubGlobal('fetch', offlineFetch);
});

afterEach(async () => {
  await Promise.all(bots.splice(0).map((bot) => bot.shutdown()));
  for (const directory of temporary.splice(0)) rmSync(directory, { recursive: true, force: true });
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function runtime(accountId = 'bot-a', state = createMemoryState()) {
  const adapter = new WeixinAdapter({ accountId, token: `synthetic-${accountId}`,
    baseUrl: 'https://offline.example.invalid', polling: { enabled: false }, fetchFn: offlineFetch });
  const bot = new Chat({ userName: 'offline', adapters: { weixin: adapter }, state, logger: 'silent' });
  const handler = vi.fn(async (_thread: unknown, _message: { author: { userId: string } }) => {});
  bot.onNewMention(handler);
  bots.push(bot);
  await bot.initialize();
  return { bot, adapter, state, handler, accountId };
}

async function receive(r: Awaited<ReturnType<typeof runtime>>, sender = 'owner', token = 'context-a',
  overrides: Record<string, unknown> = {}) {
  pending.set(`Bearer synthetic-${r.accountId}`, [{ message_id: `message-${r.accountId}-${sender}`,
    message_type: 1, message_state: 2, from_user_id: sender, to_user_id: r.accountId,
    item_list: [{ type: 1, text_item: { text: 'offline message' } }], context_token: token, ...overrides }]);
  await r.adapter.pollOnce();
}
const thread = (r: Awaited<ReturnType<typeof runtime>>, userId = 'owner') =>
  r.adapter.encodeThreadId({ accountId: r.accountId, userId });
const sends = () => calls.filter((call) => call.path === '/ilink/bot/sendmessage');

function qrFixture(status: Record<string, unknown>) {
  const requests: Array<{ url: URL; method?: string; body?: BodyInit | null }> = [];
  const fetchFn = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    if (url.origin !== 'https://ilinkai.weixin.qq.com') throw new Error('Unexpected QR fixture URL');
    requests.push({ url, method: init?.method, body: init?.body });
    if (url.pathname === '/ilink/bot/get_bot_qrcode') {
      return Response.json({ qrcode: 'synthetic-not-a-qr', qrcode_img_content: 'synthetic-not-an-image' });
    }
    if (url.pathname === '/ilink/bot/get_qrcode_status') return Response.json(status);
    throw new Error('Unexpected QR fixture endpoint');
  }) as typeof fetch;
  return { fetchFn, requests };
}

describe('Weixin 0.1.4 owner and PIN behavior — characterization', () => {
  it('returns scanned owner metadata but requests no force-PIN body', async () => {
    const qr = qrFixture({ status: 'confirmed', ilink_bot_id: 'bot-a', bot_token: 'synthetic-token',
      ilink_user_id: 'scanned-owner' });
    const result = await startWeixinLogin({ fetchFn: qr.fetchFn });
    expect(result.userId).toBe('scanned-owner');
    expect(qr.requests[0].method).toBe('GET');
    expect(qr.requests[0].body).toBeUndefined();
    expect(qr.requests[0].url.searchParams.has('bot_spam_business_data')).toBe(false);
  });

  it('accepts confirmed credentials with missing owner metadata', async () => {
    const qr = qrFixture({ status: 'confirmed', ilink_bot_id: 'bot-a', bot_token: 'synthetic-token' });
    await expect(startWeixinLogin({ fetchFn: qr.fetchFn })).resolves.toMatchObject({
      accountId: 'bot-a', userId: undefined,
    });
  });

  it('returns a different scanned owner without comparing a trusted expected owner', async () => {
    const expectedOwner = 'trusted-owner';
    const qr = qrFixture({ status: 'confirmed', ilink_bot_id: 'bot-a', bot_token: 'synthetic-token',
      ilink_user_id: 'different-scanned-owner' });
    const result = await startWeixinLogin({ fetchFn: qr.fetchFn });
    expect(result.userId).toBe('different-scanned-owner');
    expect(result.userId).not.toBe(expectedOwner);
  });

  it('still rejects confirmed login without a bot credential', async () => {
    const qr = qrFixture({ status: 'confirmed', ilink_bot_id: 'bot-a', ilink_user_id: 'owner' });
    await expect(startWeixinLogin({ fetchFn: qr.fetchFn })).rejects.toThrow('without accountId or token');
  });

  it.each(['need_verifycode', 'confirmed_but_need_verifycode'])(
    'does not handle the PIN status %s and times out offline', async (status) => {
      vi.useFakeTimers();
      const qr = qrFixture({ status });
      const rejected = expect(startWeixinLogin({ fetchFn: qr.fetchFn, timeoutMs: 1000 }))
        .rejects.toThrow('Weixin QR login timed out');
      await vi.runAllTimersAsync();
      await rejected;
      expect(qr.requests).toHaveLength(2);
      expect(qr.requests.every((request) => !request.url.searchParams.has('verify_code'))).toBe(true);
    });

  it('passes a wrong-owner inbound message to application code and caches its token', async () => {
    const r = await runtime();
    // The trusted scanned owner is "owner"; 0.1.4 has no owner policy field.
    await receive(r, 'stranger', 'synthetic-stranger-context');
    expect(r.handler).toHaveBeenCalledOnce();
    expect(r.handler.mock.calls[0][1].author.userId).toBe('stranger');
    expect(await r.state.get('weixin:bot-a:stranger:context_token')).toBe('synthetic-stranger-context');
  });

  it('initializes with no owner metadata and admits ordinary inbound messages', async () => {
    const qr = qrFixture({ status: 'confirmed', ilink_bot_id: 'bot-a', bot_token: 'synthetic-token' });
    const login = await startWeixinLogin({ fetchFn: qr.fetchFn });
    expect(login.userId).toBeUndefined();
    const r = await runtime(login.accountId);
    await receive(r, 'any-sender');
    expect(r.handler).toHaveBeenCalledOnce();
  });
});

describe('Weixin 0.1.4 context scope and StateAdapter contract', () => {
  it('keeps tokens for the same user separate across ordinary account IDs', async () => {
    const shared = createMemoryState();
    const a = await runtime('bot-a', shared);
    const b = await runtime('bot-b', shared);
    await receive(a, 'owner', 'context-a');
    await receive(b, 'owner', 'context-b');
    await a.adapter.postMessage(thread(a), 'reply a');
    await b.adapter.postMessage(thread(b), 'reply b');
    expect(sends().map((call) => [call.authorization, call.body.msg.context_token])).toEqual([
      ['Bearer synthetic-bot-a', 'context-a'], ['Bearer synthetic-bot-b', 'context-b'],
    ]);
  });

  it('keeps two users separate within one account', async () => {
    const r = await runtime();
    await receive(r, 'owner', 'owner-context');
    await receive(r, 'other', 'other-context');
    await r.adapter.postMessage(thread(r, 'owner'), 'reply');
    await r.adapter.postMessage(thread(r, 'other'), 'reply');
    expect(sends().map((call) => call.body.msg.context_token)).toEqual(['owner-context', 'other-context']);
  });

  it('rejects a foreign-account thread on postMessage before provider I/O', async () => {
    const r = await runtime('bot-a');
    await receive(r);
    const foreign = r.adapter.encodeThreadId({ accountId: 'bot-b', userId: 'owner' });
    await expect(r.adapter.postMessage(foreign, 'reply')).rejects.toThrow('does not match adapter accountId');
    expect(sends()).toEqual([]);
  });

  it('characterization: startTyping ignores foreign thread account but uses the current account token', async () => {
    const r = await runtime('bot-a');
    await receive(r, 'owner', 'context-a');
    await r.state.set('weixin:bot-b:owner:context_token', 'context-b');
    await r.adapter.startTyping(r.adapter.encodeThreadId({ accountId: 'bot-b', userId: 'owner' }));
    const config = calls.find((call) => call.path === '/ilink/bot/getconfig');
    expect(config).toMatchObject({ authorization: 'Bearer synthetic-bot-a',
      body: { ilink_user_id: 'owner', context_token: 'context-a' } });
    expect(calls.some((call) => call.path === '/ilink/bot/sendtyping')).toBe(true);
    expect(JSON.stringify(calls)).not.toContain('context-b');
  });

  it('characterization: a misrouted inbound recipient can contaminate the configured account token', async () => {
    const r = await runtime('bot-a');
    // Synthetic transport violation: this does not establish that iLink sends
    // another account's messages through an authenticated polling connection.
    await receive(r, 'owner', 'foreign-context', { to_user_id: 'bot-b' });
    await r.adapter.postMessage(thread(r), 'reply');
    expect(sends()[0]).toMatchObject({ authorization: 'Bearer synthetic-bot-a',
      body: { msg: { context_token: 'foreign-context' } } });
    expect(r.handler).toHaveBeenCalledOnce();
  });

  it('reads external state again for each send instead of retaining a stale private token cache', async () => {
    const r = await runtime();
    await receive(r, 'owner', 'old-context');
    await r.adapter.postMessage(thread(r), 'first');
    await r.state.set('weixin:bot-a:owner:context_token', 'new-context');
    await r.adapter.postMessage(thread(r), 'second');
    expect(sends().map((call) => call.body.msg.context_token)).toEqual(['old-context', 'new-context']);
  });

  it('characterization: a missing token does not block sending locally', async () => {
    const r = await runtime();
    await r.adapter.postMessage(thread(r), 'reply without prior inbound');
    expect(sends()).toHaveLength(1);
    expect(sends()[0].body.msg).not.toHaveProperty('context_token');
  });

  it('expires cached context at the adapter 30-day TTL; this is not a server validity guarantee', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(0);
    const r = await runtime();
    await receive(r);
    vi.setSystemTime(30 * 24 * 60 * 60 * 1000);
    await r.adapter.postMessage(thread(r), 'reply');
    expect(sends()[0].body.msg).not.toHaveProperty('context_token');
  });
});

describe('Weixin 0.1.4 actual OS-process restart — offline state fixture', () => {
  function restart(mode: 'memory' | 'durable', replyAccount = 'bot-a') {
    const directory = mkdtempSync(join(tmpdir(), 'weixin-restart-'));
    temporary.push(directory);
    const fixture = fileURLToPath(new URL('./fixtures/weixin-restart.mjs', import.meta.url));
    const file = join(directory, 'state.json');
    const run = (action: 'seed' | 'reply', accountId: string) => JSON.parse(execFileSync(process.execPath,
      [fixture, mode, action, file, accountId], { encoding: 'utf8', timeout: 10000 }));
    const seed = run('seed', 'bot-a');
    const reply = run('reply', replyAccount);
    expect(seed.pid).not.toBe(reply.pid);
    expect(seed.cached).toBe('synthetic-restart-context');
    expect(seed.calls).toEqual(['/ilink/bot/getupdates']);
    expect(reply.calls).toEqual(['/ilink/bot/sendmessage']);
    return reply;
  }

  it('loses memory-state context across distinct OS processes and attempts a tokenless send', () => {
    const reply = restart('memory');
    expect(reply.cached).toBeNull();
    expect(reply.sentHasContext).toBe(false);
  });

  it('recovers context in a new OS process when the same durable StateAdapter data is supplied', () => {
    const reply = restart('durable');
    expect(reply.sentHasContext).toBe(true);
    expect(reply.sentContext).toBe('synthetic-restart-context');
  });

  it('does not recover account A context into account B after an OS-process restart', () => {
    const reply = restart('durable', 'bot-b');
    expect(reply.cached).toBeNull();
    expect(reply.sentHasContext).toBe(false);
  });
});
