import { createServer, type Server } from 'node:http';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { createTelegramServer } from '../examples/chat-sdk/telegram-server.mjs';
import { readTelegramConfig } from '../examples/chat-sdk/telegram-config.mjs';
import { botApi, runTelegramAdmin, verifyTelegramIdentity } from '../examples/chat-sdk/telegram-admin.mjs';

// Real loopback HTTP on both sides of the real pinned SDK, never api.telegram.org.
const env = { TELEGRAM_BOT_TOKEN: '123:OFFLINE_ONLY', TELEGRAM_BOT_USERNAME: 'testbot',
  TELEGRAM_WEBHOOK_SECRET_TOKEN: 'offline-secret',
  TELEGRAM_GRANTS: '[{"userId":"42","chatId":"42"}]' };
const config = () => readTelegramConfig(env);
let api: Server;
let apiUrl: string;
let calls: Array<{ method: string; body: any }>;
let logs: string[];
let failure: 'none' | 'reject' | 'disconnect';
let identityId: number;
let registeredUrl: string;
const runtimes: Awaited<ReturnType<typeof createTelegramServer>>[] = [];
async function listen(server: Server) {
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('missing address');
  return `http://127.0.0.1:${address.port}`;
}
beforeEach(async () => {
  calls = []; logs = []; failure = 'none'; identityId = 123; registeredUrl = '';
  api = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString());
    const method = req.url!.split('/').at(-1)!;
    calls.push({ method, body });
    if (method === 'sendMessage' && failure === 'disconnect') { res.destroy(); return; }
    res.setHeader('content-type', 'application/json');
    if (method === 'sendMessage' && failure === 'reject') {
      res.writeHead(403); res.end(JSON.stringify({ ok: false, error_code: 403,
        description: `PRIVATE ${env.TELEGRAM_BOT_TOKEN}` })); return;
    }
    let result;
    if (method === 'getMe') result = { id: identityId, username: 'testbot', is_bot: true, first_name: 'Test' };
    else if (method === 'sendChatAction') result = true;
    else if (method === 'sendMessage') result = { message_id: 500, date: 1, text: body.text,
      chat: { id: Number(body.chat_id), type: 'private' } };
    else if (method === 'getWebhookInfo') result = { url: registeredUrl, pending_update_count: 0 };
    else if (method === 'getUpdates') result = [update()];
    else if (method === 'setWebhook') { registeredUrl = body.url; result = true; }
    else { res.writeHead(500); res.end('{"ok":false}'); return; }
    res.end(JSON.stringify({ ok: true, result }));
  });
  apiUrl = await listen(api);
});
afterEach(async () => {
  await Promise.all(runtimes.splice(0).map((r) => r.close()));
  await new Promise<void>((resolve) => api.close(() => resolve()));
});
async function start(options = {}, overrides = {}) {
  const runtime = await createTelegramServer({ ...config(), ...overrides }, {
    apiUrl, log: (line: string) => logs.push(line), ...options,
  });
  runtimes.push(runtime);
  const url = await listen(runtime.server);
  calls.length = 0;
  return { ...runtime, url };
}
function update(id = 1, message = {}) {
  return { update_id: id, message: { message_id: id + 6, date: 1, text: 'hello acceptance',
    from: { id: 42, is_bot: false, first_name: 'Owner' },
    chat: { id: 42, type: 'private' }, ...message } };
}
function post(url: string, body: unknown = update(), secret: string | null = 'offline-secret') {
  return fetch(`${url}/webhooks/telegram`, { method: 'POST',
    headers: { 'content-type': 'application/json',
      ...(secret === null ? {} : { 'x-telegram-bot-api-secret-token': secret }) },
    body: typeof body === 'string' ? body : JSON.stringify(body) });
}
const sends = () => calls.filter((c) => c.method === 'sendMessage');

describe('standalone Telegram text pilot over isolated HTTP', () => {
  it('starts, reports volatile health, and sends a reply through the official adapter', async () => {
    const { url } = await start();
    expect(await (await fetch(`${url}/healthz`)).json()).toMatchObject({ state: 'memory', realRoundTrip: 'unverified' });
    expect((await post(url)).status).toBe(200);
    expect(sends()).toHaveLength(1);
    expect(sends()[0].body).toMatchObject({ chat_id: '42', text: 'Echo: hello acceptance' });
    expect(logs.join('\n')).not.toContain('hello acceptance');
  });
  it.each([null, 'wrong'])('rejects secret %s without any provider side effect', async (secret) => {
    const { url } = await start();
    expect((await post(url, update(), secret)).status).toBe(401);
    expect(calls).toEqual([]);
  });
  it.each([
    { from: { id: 99, is_bot: false } }, { chat: { id: -10099, type: 'supergroup' } },
    { from: { id: 42, is_bot: true } },
  ])('denies %j before even a typing action', async (message) => {
    const { url } = await start();
    const response = await post(url, update(1, message));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: 'denied' });
    expect(calls).toEqual([]);
  });
  it('enforces topic scope and preserves the permitted reply topic', async () => {
    const { url } = await start({}, { grants: [{ userId: '42', chatId: '-10042', topicId: 9 }] });
    const message = { chat: { id: -10042, type: 'supergroup' }, message_thread_id: 10 };
    expect(await (await post(url, update(1, message))).json()).toEqual({ status: 'denied' });
    expect(calls).toEqual([]);
    expect((await post(url, update(2, { ...message, message_thread_id: 9 }))).status).toBe(200);
    expect(sends()[0].body).toMatchObject({ chat_id: '-10042', message_thread_id: 9 });
  });
  it('suppresses simultaneous and sequential duplicate deliveries in one instance', async () => {
    const { url } = await start();
    expect((await Promise.all([post(url), post(url)])).map((r) => r.status)).toEqual([200, 200]);
    expect((await post(url)).status).toBe(200);
    expect(sends()).toHaveLength(1);
  });
  it.each(['{', 'null', '{"update_id":"1"}', JSON.stringify(update(1, { from: {} }))])(
    'rejects malformed input without sending: %s', async (body) => {
      const { url } = await start();
      expect((await post(url, body)).status).toBe(400); expect(calls).toEqual([]);
    });
  it('bounds the body and routes methods/paths before SDK dispatch', async () => {
    const { url } = await start();
    expect((await post(url, 'x'.repeat(65537))).status).toBe(413);
    expect((await fetch(`${url}/webhooks/telegram`)).status).toBe(405);
    expect((await fetch(`${url}/missing`)).status).toBe(404);
    expect(calls).toEqual([]);
  });
  it('acknowledges unsupported updates and /start without claiming a reply', async () => {
    const { url } = await start();
    for (const body of [{ update_id: 2, callback_query: {} }, update(3, {
      text: '/start', entities: [{ type: 'bot_command', offset: 0, length: 6 }],
    })]) {
      expect(await (await post(url, body)).json()).toMatchObject({ status: 'ignored' });
    }
    expect(calls).toEqual([]);
  });
  it.each(['reject', 'disconnect'] as const)(
    'reports %s as failed/unknown, redacts credentials, and never calls a retry recovery success', async (mode) => {
      const { url } = await start(); failure = mode;
      const response = await post(url);
      expect(response.status).toBe(502);
      expect(await response.json()).toEqual({ error: 'reply_failed_or_unknown', automaticRecovery: false });
      failure = 'none'; expect((await post(url)).status).toBe(200);
      expect(sends()).toHaveLength(1); // SDK claimed the ID before the failed send.
      expect(logs.join('\n')).toContain('reply_failed_or_unknown');
      expect(logs.join('\n')).not.toContain(env.TELEGRAM_BOT_TOKEN);
      expect(logs.join('\n')).not.toContain('PRIVATE');
    });
  it('reports a stalled handler as unknown without launching another send', async () => {
    let release!: () => void;
    const { url } = await start({ taskTimeoutMs: 20, respond: () => new Promise<void>((r) => { release = r; }) });
    expect((await post(url)).status).toBe(502);
    release();
    expect((await post(url)).status).toBe(200);
    expect(sends()).toHaveLength(0);
  });
  it('loses dedupe on a new runtime: this is evidence against a recovery guarantee', async () => {
    const first = await start(); await post(first.url);
    const second = await start(); await post(second.url);
    expect(sends()).toHaveLength(1); // start() cleared the previous instance's calls.
    expect(logs.filter((s) => s.includes('webhook_completed'))).toHaveLength(2);
  });
  it('fails identity preflight instead of listening with a mismatched bot', async () => {
    identityId = 999;
    await expect(start()).rejects.toThrow('identity');
  });
  it('returns sanitized operator API errors', async () => {
    await expect(botApi(config(), 'notSupported', {}, apiUrl)).rejects.toThrow('notSupported failed');
    await expect(verifyTelegramIdentity({ ...config(), userName: 'wrongbot' }, apiUrl)).rejects.toThrow('identity');
  });
  it('discovers only grant IDs and explicitly registers a verified webhook without dropping updates', async () => {
    const options = { apiUrl, log: (line: string) => logs.push(line) };
    await runTelegramAdmin('ids', { ...env, TELEGRAM_GRANTS: '' }, options);
    expect(JSON.parse(logs[0])).toEqual([{ userId: '42', chatId: '42' }]);
    expect(logs.join('\n')).not.toContain('hello acceptance');
    expect(calls.find((c) => c.method === 'getUpdates')?.body).not.toHaveProperty('offset');
    await runTelegramAdmin('register', { ...env, TELEGRAM_WEBHOOK_URL: 'https://pilot.example/webhooks/telegram' }, options);
    expect(calls.find((c) => c.method === 'setWebhook')?.body).toEqual({
      url: 'https://pilot.example/webhooks/telegram', secret_token: 'offline-secret',
      allowed_updates: ['message'], max_connections: 1, drop_pending_updates: false,
    });
    await runTelegramAdmin('check', env, options);
    expect(JSON.parse(logs.at(-1)!)).toMatchObject({ identity: 'verified', webhookRegistered: true });
    expect(logs.join('\n')).not.toContain('offline-secret');
  });
  it('refuses to inspect polling updates or replace another registered webhook', async () => {
    registeredUrl = 'https://existing.example/webhook';
    const options = { apiUrl, log: () => {} };
    await expect(runTelegramAdmin('ids', env, options)).rejects.toThrow('without a webhook');
    await expect(runTelegramAdmin('register', { ...env,
      TELEGRAM_WEBHOOK_URL: 'https://pilot.example/webhooks/telegram' }, options)).rejects.toThrow('different webhook');
    expect(calls.some((c) => ['setWebhook', 'deleteWebhook', 'getUpdates'].includes(c.method))).toBe(false);
  });
  it('starts the actual CLI with an env file, echoes over HTTP and stops on SIGTERM', async () => {
    const folder = await mkdtemp(join(tmpdir(), 'telegram-pilot-'));
    const reservation = createServer();
    const reservedUrl = await listen(reservation);
    const port = new URL(reservedUrl).port;
    await new Promise<void>((r) => reservation.close(() => r()));
    const envPath = join(folder, 'pilot.env');
    const preload = join(folder, 'offline-transport.mjs');
    await writeFile(envPath, Object.entries({ ...env, TELEGRAM_PORT: port }).map(([k, v]) => `${k}='${v}'`).join('\n'));
    // Replace only the network destination; run the unmodified CLI/env-file lifecycle.
    await writeFile(preload, `const original = globalThis.fetch;
      globalThis.fetch = (url, init) => {
        if (!String(url).startsWith('https://api.telegram.org/')) throw new Error('unexpected outbound host');
        return original(String(url).replace('https://api.telegram.org', ${JSON.stringify(apiUrl)}), init);
      };`);
    const childEnv = Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.startsWith('TELEGRAM_')));
    const child = spawn(process.execPath, ['--env-file', envPath, '--import', preload,
      resolve('examples/chat-sdk/telegram-server.mjs')], { env: childEnv, stdio: ['ignore', 'pipe', 'pipe'] });
    let output = ''; let errors = '';
    child.stderr.on('data', (data) => { errors += data; });
    const exited = new Promise((r) => child.once('exit', (code) => r(code)));
    try {
      await new Promise<void>((r, reject) => {
        const timer = setTimeout(() => reject(new Error('CLI startup timeout')), 3000);
        child.stdout.on('data', (data) => {
          output += data;
          if (output.includes('listening')) { clearTimeout(timer); r(); }
        });
        child.once('exit', () => { clearTimeout(timer); reject(new Error('CLI exited before ready')); });
      });
      expect((await post(reservedUrl)).status).toBe(200);
      expect(sends()).toHaveLength(1);
      child.kill('SIGTERM');
      expect(await exited).toBe(0);
      expect(errors).toBe('');
      expect(output).not.toContain(env.TELEGRAM_BOT_TOKEN);
    } finally {
      if (child.exitCode === null) { child.kill('SIGKILL'); await exited; }
      await rm(folder, { recursive: true, force: true });
    }
  });
  it.each([
    { TELEGRAM_BOT_TOKEN: '' }, { TELEGRAM_WEBHOOK_SECRET_TOKEN: '' }, { TELEGRAM_GRANTS: '[]' },
    { TELEGRAM_GRANTS: '[{"userId":42,"chatId":"42"}]' }, { TELEGRAM_PORT: 'abc' },
  ])('fails closed on invalid configuration %j', (override) => {
    expect(() => readTelegramConfig({ ...env, ...override })).toThrow();
  });
  it('the actual CLI exits nonzero with a useful missing-variable error', () => {
    const result = spawnSync(process.execPath, ['examples/chat-sdk/telegram-server.mjs'], {
      env: { ...process.env, TELEGRAM_BOT_TOKEN: '' }, encoding: 'utf8', timeout: 5000,
    });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('TELEGRAM_BOT_TOKEN is required');
    expect(result.stderr).not.toContain('OFFLINE_ONLY');
  });
});
