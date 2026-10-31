import { afterEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { AgentTurnResult } from '../src/core/index.js';
import { TelegramApiError } from '../src/telegram/client.js';
import { TelegramPollingHost, TelegramPollStore } from '../src/telegram/polling-host.js';

const stores: TelegramPollStore[] = [];
const directories: string[] = [];
afterEach(() => {
  stores.forEach((store) => store.close()); stores.length = 0;
  directories.forEach((path) => rmSync(path, { recursive: true, force: true })); directories.length = 0;
});
function open(path = ':memory:', accountId = '123') {
  const store = new TelegramPollStore(path, accountId); stores.push(store); return store;
}
function databasePath() {
  const path = mkdtempSync(join(tmpdir(), 'channel-kit-test-')); directories.push(path);
  return join(path, 'state.sqlite');
}
function update(id = 1, user = 7) {
  return { update_id: id, message: { message_id: 10, text: 'Hello',
    from: { id: user }, chat: { id: -100, type: 'supergroup' }, message_thread_id: 9 } };
}
const grant = { platform: 'telegram', accountId: '123', conversationId: '-100', senderId: '7', threadId: '9' };
function setup(options: { store?: TelegramPollStore; updates?: Record<string, unknown>[]; authorized?: boolean } = {}) {
  const store = options.store ?? open();
  const client = {
    getMe: vi.fn(async () => ({ id: '123', isBot: true })),
    getUpdates: vi.fn(async () => options.updates ?? [update()]),
    reply: vi.fn(async () => ({ messageId: '20', chatId: '-100', threadId: '9' })),
  };
  const runner = { name: 'test', run: vi.fn(async () => new AgentTurnResult({ runner: 'test', outputText: 'Answer' })) };
  const host = new TelegramPollingHost({ client, store, runner,
    grants: options.authorized === false ? [] : [grant] });
  return { client, store, runner, host };
}

describe('Telegram durable echo/runner host', () => {
  it('checks bot identity and exact scoped authorization before running and replies to the original topic', async () => {
    const { host, runner, client, store } = setup();
    await expect(host.pollOnce()).resolves.toEqual([{ updateId: '1', disposition: 'delivered', messageId: '20' }]);
    expect(client.getMe).toHaveBeenCalledOnce();
    const request = runner.run.mock.calls[0][0];
    expect(request.prompt).toBe('Hello');
    expect(request.context.platform).toBe('telegram');
    expect(JSON.parse(request.sessionId!)).toEqual(['channel-kit.session.v1', 'telegram', '123', '-100', '9', '7']);
    expect(client.reply.mock.calls[0][0]).toMatchObject({ conversationId: '-100', threadId: '9', messageId: '10' });
    expect(store.getOffset()).toBe('2');
  });
  it('denies ungranted senders without invoking an agent or sending a rejection', async () => {
    const { host, runner, client, store } = setup({ authorized: false });
    expect((await host.pollOnce())[0].disposition).toBe('denied');
    expect(runner.run).not.toHaveBeenCalled(); expect(client.reply).not.toHaveBeenCalled();
    expect(store.getOffset()).toBe('2');
  });
  it('does not reuse an authorized user grant for a different sender', async () => {
    const { host, runner } = setup({ updates: [update(1, 8)] });
    expect((await host.pollOnce())[0].disposition).toBe('denied'); expect(runner.run).not.toHaveBeenCalled();
  });
  it('deduplicates repeated provider updates within one batch', async () => {
    const { host, runner, client } = setup({ updates: [update(), update()] });
    await host.pollOnce(); expect(runner.run).toHaveBeenCalledOnce(); expect(client.reply).toHaveBeenCalledOnce();
  });
  it('recovers delivered receipts and the polling cursor after reopening the store', async () => {
    const path = databasePath(); const first = setup({ store: open(path) });
    await first.host.pollOnce(); first.store.close();
    const second = setup({ store: open(path) }); await second.host.pollOnce();
    expect(second.client.getUpdates.mock.calls[0][0]).toMatchObject({ offset: '2' });
    expect(second.runner.run).not.toHaveBeenCalled(); expect(second.client.reply).not.toHaveBeenCalled();
    expect(second.store.getReceipt('1')?.disposition).toBe('delivered');
  });
  it.each(['running', 'sending'] as const)('preserves interrupted %s as unknown instead of replaying', async (phase) => {
    const path = databasePath(); const first = open(path); first.claim('1');
    if (phase === 'sending') first.markSending('1'); first.close();
    const { host, store, runner, client } = setup({ store: open(path) });
    expect((await host.pollOnce())[0].disposition).toBe(phase === 'sending' ? 'unknown' : 'execution_unknown');
    expect(runner.run).not.toHaveBeenCalled(); expect(client.reply).not.toHaveBeenCalled();
    expect(store.getOffset()).toBe('2');
  });
  it('persists a timeout as unknown and never resends on a later poll', async () => {
    const { host, client, store } = setup();
    client.reply.mockRejectedValueOnce(new Error('response lost'));
    expect((await host.pollOnce())[0].disposition).toBe('unknown');
    await host.pollOnce(); expect(client.reply).toHaveBeenCalledOnce();
    expect(store.getReceipt('1')?.disposition).toBe('unknown');
  });
  it('persists explicit API rejection as failure without claiming delivery', async () => {
    const { host, client, store } = setup(); client.reply.mockRejectedValueOnce(new TelegramApiError('sendMessage', 403, 403));
    expect((await host.pollOnce())[0].disposition).toBe('failed');
    expect(store.getReceipt('1')?.messageId).toBeUndefined();
  });
  it.each(['10', undefined])('does not claim delivery when the returned topic is %s', async (threadId) => {
    const { host, client } = setup();
    client.reply.mockResolvedValueOnce({ messageId: '20', chatId: '-100', threadId });
    expect((await host.pollOnce())[0].disposition).toBe('unknown');
    await host.pollOnce(); expect(client.reply).toHaveBeenCalledOnce();
  });
  it('marks an oversized response as definitely unsent before recording network intent', async () => {
    const { host, client, runner, store } = setup();
    runner.run.mockResolvedValueOnce(new AgentTurnResult({ runner: 'test', outputText: 'x'.repeat(4097) }));
    expect((await host.pollOnce())[0].disposition).toBe('failed');
    expect(client.reply).not.toHaveBeenCalled();
    expect(store.getReceipt('1')?.disposition).toBe('failed');
  });
  it('does not send when execution fails or is cancelled before delivery', async () => {
    const { host, runner, client } = setup(); const abort = new AbortController();
    runner.run.mockImplementationOnce(async () => { abort.abort(); return new AgentTurnResult({ runner: 'test', outputText: 'Answer' }); });
    expect((await host.pollOnce({ signal: abort.signal }))[0].disposition).toBe('run_failed');
    expect(client.reply).not.toHaveBeenCalled();
  });
  it('refuses to consume updates with a different connected bot', async () => {
    const { host, client } = setup(); client.getMe.mockResolvedValueOnce({ id: '456', isBot: true });
    await expect(host.pollOnce()).rejects.toThrow('identity'); expect(client.getUpdates).not.toHaveBeenCalled();
  });
  it('ignores callback data instead of treating buttons as agent prompts', async () => {
    const item = update(); const { host, runner } = setup({ updates: [{ update_id: 1,
      callback_query: { id: 'cb', data: 'run-shell', from: { id: 7 }, message: item.message } }] });
    expect((await host.pollOnce())[0].disposition).toBe('ignored'); expect(runner.run).not.toHaveBeenCalled();
  });
  it('rejects concurrent consumers and bot changes without modifying existing receipts', () => {
    const path = databasePath(); const first = open(path); first.claim('1');
    expect(() => open(path)).toThrow('live consumer'); first.close();
    expect(() => open(path, '456')).toThrow('different bot');
    expect(open(path).getReceipt('1')?.disposition).toBe('execution_unknown');
  });
  it('never advances the cursor for an invalid provider update ID', async () => {
    const { host, store } = setup({ updates: [{ update_id: Number.MAX_SAFE_INTEGER + 1 }] });
    await expect(host.pollOnce()).rejects.toThrow('update_id'); expect(store.getOffset()).toBeUndefined();
  });
  it('records intent before the send, and stores no prompt or response text', async () => {
    const { host, client, store } = setup();
    client.reply.mockImplementationOnce(async () => {
      expect(store.getReceipt('1')?.disposition).toBe('sending');
      return { messageId: '20', chatId: '-100', threadId: '9' };
    });
    await host.pollOnce();
    expect(JSON.stringify(store.getReceipt('1'))).not.toContain('Hello');
    expect(JSON.stringify(store.getReceipt('1'))).not.toContain('Answer');
  });
});
