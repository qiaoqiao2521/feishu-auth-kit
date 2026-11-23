// Offline subprocess fixture, not a production storage implementation.
// The real adapter and SDK run in separate OS processes. Only get/set is backed
// by a JSON file to exercise the existing StateAdapter persistence contract.
import { readFileSync, writeFileSync } from 'node:fs';
import { Chat } from 'chat';
import { createMemoryState } from '@chat-adapter/state-memory';
import { WeixinAdapter } from 'chat-adapter-weixin';

const [mode, action, file, accountId = 'bot-a'] = process.argv.slice(2);
if (!['memory', 'durable'].includes(mode) || !['seed', 'reply'].includes(action)) {
  throw new Error('Invalid offline fixture arguments');
}
const state = createMemoryState();
if (mode === 'durable') {
  const read = () => {
    try { return JSON.parse(readFileSync(file, 'utf8')); }
    catch (error) { if (error.code === 'ENOENT') return {}; throw error; }
  };
  state.get = async (key) => {
    const item = read()[key];
    return item && (item.expiresAt === null || item.expiresAt > Date.now()) ? item.value : null;
  };
  state.set = async (key, value, ttlMs) => {
    const data = read();
    data[key] = { value, expiresAt: ttlMs ? Date.now() + ttlMs : null };
    writeFileSync(file, JSON.stringify(data), { mode: 0o600 });
  };
}
const calls = [];
globalThis.fetch = async (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input));
  if (url.origin !== 'https://offline.example.invalid') throw new Error('Unexpected fixture URL');
  const body = JSON.parse(String(init?.body ?? '{}'));
  calls.push({ path: url.pathname, body });
  if (url.pathname === '/ilink/bot/getupdates') {
    return Response.json({ ret: 0, get_updates_buf: 'synthetic-cursor', msgs: [{
      message_id: 'fixture-1', message_type: 1, message_state: 2,
      from_user_id: 'owner', to_user_id: accountId,
      context_token: 'synthetic-restart-context',
      item_list: [{ type: 1, text_item: { text: 'offline message' } }],
    }] });
  }
  if (url.pathname === '/ilink/bot/sendmessage') return Response.json({ ret: 0 });
  throw new Error('Unexpected fixture endpoint');
};
const adapter = new WeixinAdapter({ accountId, token: 'synthetic-bot-credential',
  baseUrl: 'https://offline.example.invalid', polling: { enabled: false }, fetchFn: globalThis.fetch });
const bot = new Chat({ userName: 'offline', adapters: { weixin: adapter }, state, logger: 'silent' });
bot.onNewMention(async () => {});
await bot.initialize();
if (action === 'seed') await adapter.pollOnce();
else await adapter.postMessage(adapter.encodeThreadId({ accountId, userId: 'owner' }), 'offline reply');
const sent = calls.find((call) => call.path === '/ilink/bot/sendmessage');
const cached = await state.get(`weixin:${accountId}:owner:context_token`);
await bot.shutdown();
process.stdout.write(JSON.stringify({ pid: process.pid, action, cached,
  sentContext: sent?.body.msg.context_token ?? null,
  sentHasContext: sent ? Object.hasOwn(sent.body.msg, 'context_token') : null,
  calls: calls.map((call) => call.path) }));
