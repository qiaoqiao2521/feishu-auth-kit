import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Chat } from 'chat';
import { createMemoryState } from '@chat-adapter/state-memory';

// Compatibility level: exact released exports + constructors against Chat 4.43.
// Nothing initializes, authenticates, connects, polls, or sends a message.
let deniedFetch: ReturnType<typeof vi.fn>;
let socketAttempts: unknown[][];

beforeEach(() => {
  vi.useFakeTimers();
  deniedFetch = vi.fn(async () => { throw new Error('Candidate construction attempted HTTP'); });
  socketAttempts = [];
  vi.stubGlobal('fetch', deniedFetch);
  vi.stubGlobal('WebSocket', class ForbiddenWebSocket {
    constructor(...args: unknown[]) {
      socketAttempts.push(args);
      throw new Error('Candidate construction attempted WebSocket');
    }
  });
});

afterEach(() => {
  try {
    expect(deniedFetch).not.toHaveBeenCalled();
    expect(socketAttempts).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  } finally {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  }
});

describe('reuse candidates: released exports and construction only', () => {
  it('constructs Lark 0.3.0 and composes it with Chat without opening its channel', async () => {
    const { createLarkAdapter } = await import('@larksuite/vercel-chat-adapter');
    const lark = createLarkAdapter({
      appId: 'offline-lark-app', appSecret: 'offline-lark-secret', domain: 'feishu',
    });
    const bot = new Chat({
      userName: 'offline-lark', adapters: { lark }, state: createMemoryState(), logger: 'silent',
    });
    expect(bot).toBeInstanceOf(Chat);
    expect(lark.decodeThreadId(lark.encodeThreadId({ chatId: 'oc_offline', rootId: 'om_root' })))
      .toEqual({ chatId: 'oc_offline', rootId: 'om_root' });
  });

  it('constructs QQ 0.1.1 and composes it with Chat without token or WS activity', async () => {
    const { createQQBotAdapter } = await import('@agentor/chat-qq');
    const qq = createQQBotAdapter({
      mode: 'websocket', appId: 'offline-qq-app', clientSecret: 'offline-qq-secret',
    });
    const bot = new Chat({
      userName: 'offline-qq', adapters: { qq }, state: createMemoryState(), logger: 'silent',
    });
    expect(bot).toBeInstanceOf(Chat);
    expect(qq.decodeThreadId(qq.encodeThreadId({ scene: 'c2c', id: 'offline-user' })))
      .toEqual({ scene: 'c2c', id: 'offline-user' });
  });

  it('constructs Weixin 0.1.4 and composes it with Chat while polling stays disabled', async () => {
    const { createWeixinAdapter } = await import('chat-adapter-weixin');
    const weixin = createWeixinAdapter({
      accountId: 'offline-weixin-account', token: 'offline-weixin-token', polling: { enabled: false },
    });
    const bot = new Chat({
      userName: 'offline-weixin', adapters: { weixin }, state: createMemoryState(), logger: 'silent',
    });
    expect(bot).toBeInstanceOf(Chat);
    expect(weixin.decodeThreadId(weixin.encodeThreadId({ accountId: 'offline-weixin-account', userId: 'offline-user' })))
      .toEqual({ accountId: 'offline-weixin-account', userId: 'offline-user' });
  });
});
