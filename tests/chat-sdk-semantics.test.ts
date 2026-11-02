import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Chat, ConsoleLogger, Message, type ConcurrencyStrategy, type MessageContext } from 'chat';
import { createTelegramAdapter } from '@chat-adapter/telegram';
import { createMemoryState } from '@chat-adapter/state-memory';

// These tests document the actual pinned SDK 4.43.0 semantics. All provider I/O
// is stubbed; the test suite does not run a bot or send Telegram messages.
const bots: Chat[] = [];
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
    const url = String(input);
    const match = /\/bot(\d+):[^/]+\/getMe$/.exec(url);
    if (!match) throw new Error('Unexpected provider call in offline SDK semantics test');
    return Response.json({ ok: true, result: { id: Number(match[1]), is_bot: true,
      first_name: 'Offline Bot', username: 'offline_bot' } });
  }));
});
afterEach(async () => {
  await Promise.all(bots.splice(0).map((bot) => bot.shutdown()));
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function runtime(accountId = '123', concurrency: ConcurrencyStrategy = 'concurrent') {
  const adapter = createTelegramAdapter({
    botToken: `${accountId}:synthetic-offline-token`, userName: 'offline_bot',
    mode: 'webhook', secretToken: 'synthetic-offline-secret', logger: new ConsoleLogger('silent'),
  });
  const state = createMemoryState();
  const bot = new Chat({ userName: 'offline_bot', adapters: { telegram: adapter }, state,
    logger: 'silent', concurrency, dedupeTtlMs: 100 });
  bots.push(bot);
  return { bot, adapter, state };
}

function incoming(adapter: ReturnType<typeof createTelegramAdapter>, id = 10, text = 'Hello') {
  return adapter.parseMessage({ message_id: id, date: 1, text,
    from: { id: 7, is_bot: false, first_name: 'Owner' },
    chat: { id: 7, type: 'private', first_name: 'Owner' } });
}

describe('pinned Chat SDK composition boundaries', () => {
  it('serializes provider raw data, so model input must select public fields explicitly', () => {
    const message = new Message({
      id: 'message', threadId: 'telegram:7', text: 'Hello',
      author: { userId: '7', userName: 'owner', fullName: 'Owner', isBot: false, isMe: false },
      formatted: { type: 'root', children: [] }, metadata: { dateSent: new Date(0), edited: false },
      raw: { privateTransportValue: 'synthetic-private-context' },
      attachments: [{ type: 'file', url: 'https://example.test/signed-secret',
        fetchMetadata: { privateTransportValue: 'synthetic-attachment-context' } }],
    });

    expect(message.toJSON().raw).toEqual({ privateTransportValue: 'synthetic-private-context' });
    expect(JSON.stringify(message)).toContain('synthetic-attachment-context');
    const modelInput = { text: message.text, messageId: message.id, senderId: message.author.userId };
    expect(JSON.stringify(modelInput)).not.toContain('synthetic-private-context');
    expect(JSON.stringify(modelInput)).not.toContain('signed-secret');
  });

  it('keeps identical platform IDs independent when each bot owns a separate SDK state instance', async () => {
    const first = runtime('123');
    const second = runtime('456');
    const firstHandler = vi.fn();
    const secondHandler = vi.fn();
    first.bot.onNewMention(firstHandler);
    second.bot.onNewMention(secondHandler);
    await Promise.all([first.bot.initialize(), second.bot.initialize()]);
    const a = incoming(first.adapter);
    const b = incoming(second.adapter);
    expect(a.id).toBe(b.id);
    expect(a.threadId).toBe(b.threadId);

    await first.state.subscribe(a.threadId);
    expect(await second.state.isSubscribed(b.threadId)).toBe(false);
    await first.state.unsubscribe(a.threadId);
    await first.bot.processMessage(first.adapter, a.threadId, a);
    await second.bot.processMessage(second.adapter, b.threadId, b);

    expect(firstHandler).toHaveBeenCalledOnce();
    expect(secondHandler).toHaveBeenCalledOnce();
  });

  it('deduplicates before handler success and permits another attempt only after the configured TTL', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(0);
    const { bot, adapter } = runtime();
    const failure = new Error('handler failed after work may already have happened');
    const handler = vi.fn().mockRejectedValueOnce(failure).mockResolvedValue(undefined);
    bot.onNewMention(handler);
    await bot.initialize();
    const message = incoming(adapter);

    await expect(bot.processMessage(adapter, message.threadId, message)).rejects.toBe(failure);
    await bot.processMessage(adapter, message.threadId, incoming(adapter));
    expect(handler).toHaveBeenCalledOnce();

    vi.setSystemTime(101);
    await bot.processMessage(adapter, message.threadId, incoming(adapter));
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('coalesces the queue to its latest message and exposes earlier text in context.skipped', async () => {
    const { bot, adapter } = runtime('123', 'queue');
    let started!: () => void;
    const firstStarted = new Promise<void>((resolve) => { started = resolve; });
    let release!: () => void;
    const firstCanFinish = new Promise<void>((resolve) => { release = resolve; });
    const seen: Array<{ text: string; skipped: string[] }> = [];
    bot.onNewMention(async (_thread, message, context?: MessageContext) => {
      seen.push({ text: message.text, skipped: context?.skipped.map((item) => item.text) ?? [] });
      if (message.text === 'first') { started(); await firstCanFinish; }
    });
    await bot.initialize();

    const initial = incoming(adapter, 10, 'first');
    const first = bot.processMessage(adapter, initial.threadId, initial);
    await firstStarted;
    const second = incoming(adapter, 11, 'second');
    const third = incoming(adapter, 12, 'third');
    await bot.processMessage(adapter, second.threadId, second);
    await bot.processMessage(adapter, third.threadId, third);
    release();
    await first;

    expect(seen).toEqual([{ text: 'first', skipped: [] }, { text: 'third', skipped: ['second'] }]);
  });
});
