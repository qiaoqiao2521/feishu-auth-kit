import { createServer } from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { createTelegramPilot } from './telegram.mjs';
import { permits, readTelegramConfig } from './telegram-config.mjs';
import { verifyTelegramIdentity } from './telegram-admin.mjs';

const MAX_BODY = 64 * 1024;
const json = (status, result) => Response.json(result, { status });

// Only the transport seam is injectable. CLI always uses the official API host.
export async function createTelegramServer(config, { apiUrl, log = console.log,
  respond = async ({ text }) => `Echo: ${text.slice(0, 1000)}`, taskTimeoutMs = 15_000 } = {}) {
  await verifyTelegramIdentity(config, apiUrl);
  const bot = createTelegramPilot({ ...config, apiUrl, respond });
  await bot.initialize();
  const handle = async (req) => {
    const path = new URL(req.url, 'http://localhost').pathname;
    if (path === '/healthz' && req.method === 'GET') {
      return json(200, { status: 'running', state: 'memory', realRoundTrip: 'unverified' });
    }
    if (path !== '/webhooks/telegram') return json(404, { error: 'not_found' });
    if (req.method !== 'POST') return json(405, { error: 'post_required' });
    const header = req.headers['x-telegram-bot-api-secret-token'];
    const supplied = Buffer.from(typeof header === 'string' ? header : '');
    const expected = Buffer.from(config.secretToken);
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
      return json(401, { error: 'invalid_webhook_secret' });
    }
    if (!/^application\/json(?:;|$)/i.test(req.headers['content-type'] ?? '')) {
      return json(415, { error: 'json_required' });
    }
    const chunks = []; let size = 0;
    for await (const chunk of req) {
      size += chunk.length;
      if (size > MAX_BODY) return json(413, { error: 'body_too_large' });
      chunks.push(chunk);
    }
    let update;
    try { update = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch {
      return json(400, { error: 'invalid_json' });
    }
    if (!Number.isSafeInteger(update?.update_id) || update.update_id < 0) {
      return json(400, { error: 'invalid_update_id' });
    }
    const message = update.message;
    // No callbacks, edits, albums, media or commands in this first acceptance target.
    if (!message || typeof message.text !== 'string' || message.media_group_id
      || (Array.isArray(message.entities) && message.entities.some((entity) => entity?.type === 'bot_command'))) {
      return json(200, { status: 'ignored', reason: 'plain_text_only' });
    }
    if (!Number.isSafeInteger(message.message_id) || !Number.isSafeInteger(message.date)
      || (message.entities !== undefined && !Array.isArray(message.entities))
      || !Number.isSafeInteger(message.from?.id) || !Number.isSafeInteger(message.chat?.id)
      || !['private', 'group', 'supergroup'].includes(message.chat?.type)
      || (message.message_thread_id !== undefined
        && (!Number.isSafeInteger(message.message_thread_id) || message.message_thread_id <= 0))) {
      return json(400, { error: 'invalid_message' });
    }
    // Check before the SDK's typing action, with the same grant policy as the handler.
    if (!permits(config.grants, message)) {
      log(JSON.stringify({ event: 'denied', updateId: update.update_id }));
      // Permanent refusal is acknowledged to stop Telegram retrying it.
      return json(200, { status: 'denied' });
    }
    const tasks = [];
    const response = await bot.webhooks.telegram(new Request('http://localhost/webhooks/telegram', {
      method: 'POST', headers: { 'content-type': 'application/json',
        'x-telegram-bot-api-secret-token': config.secretToken }, body: JSON.stringify(update),
    }), { propagateHandlerErrors: true,
      waitUntil: (task) => { tasks.push(Promise.resolve(task).then(
        () => true, () => false)); } });
    let timer;
    const completed = await Promise.race([
      Promise.all(tasks).then((results) => results.every(Boolean)),
      new Promise((resolve) => { timer = setTimeout(() => resolve(false), taskTimeoutMs); }),
    ]).finally(() => clearTimeout(timer));
    if (!completed) {
      log(JSON.stringify({ event: 'reply_failed_or_unknown', updateId: update.update_id }));
      return json(502, { error: 'reply_failed_or_unknown', automaticRecovery: false });
    }
    log(JSON.stringify({ event: 'webhook_completed', updateId: update.update_id, status: response.status }));
    return response; // A duplicate also returns 200; this alone never proves a reply.
  };
  const server = createServer(async (req, res) => {
    let response;
    try { response = await handle(req); } catch {
      log(JSON.stringify({ event: 'request_failed' }));
      response = json(500, { error: 'request_failed' });
    }
    res.writeHead(response.status, Object.fromEntries(response.headers));
    res.end(Buffer.from(await response.arrayBuffer()));
  });
  server.requestTimeout = 10_000;
  server.headersTimeout = 10_000;
  return { server, bot, async close() {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    await bot.shutdown();
  } };
}

async function main() {
  let config;
  try { config = readTelegramConfig(); } catch (error) {
    console.error(error.message); process.exitCode = 1; return;
  }
  const runtime = await createTelegramServer(config);
  try {
    await new Promise((resolve, reject) => {
      runtime.server.once('error', reject);
      runtime.server.listen(config.port, config.host, resolve);
    });
  } catch (error) { await runtime.bot.shutdown(); throw error; }
  console.log(JSON.stringify({ event: 'listening', host: config.host, port: config.port, state: 'memory' }));
  let stopping = false;
  const stop = async () => {
    if (stopping) return;
    stopping = true;
    const deadline = setTimeout(() => process.exit(1), 15_000).unref();
    await runtime.close(); clearTimeout(deadline);
  };
  process.once('SIGINT', stop);
  process.once('SIGTERM', stop);
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => { console.error('Telegram pilot failed; check configuration, port and API access. No credentials logged.'); process.exitCode = 1; });
}
