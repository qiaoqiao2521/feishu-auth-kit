/** Local single-consumer example. Requires Node 24+, pnpm build, and explicit grants. */
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { EchoRunner, CodexCliRunner } from 'feishu-auth-kit/core';
import { TelegramBotClient } from 'feishu-auth-kit/telegram';

if (Number(process.versions.node.split('.')[0]) < 24) {
  throw new Error('The optional durable Telegram host requires Node 24 or later');
}
const { TelegramPollingHost, TelegramPollStore } = await import('feishu-auth-kit/telegram-host');
const { TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_ID, TELEGRAM_GRANTS_FILE } = process.env;
if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_BOT_ID || !TELEGRAM_GRANTS_FILE) {
  throw new Error('Set TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_ID, and TELEGRAM_GRANTS_FILE locally');
}
if (!/^[1-9][0-9]*$/.test(TELEGRAM_BOT_ID)) throw new Error('TELEGRAM_BOT_ID must be the numeric bot ID');
const grants = JSON.parse(readFileSync(TELEGRAM_GRANTS_FILE, 'utf8'));
if (!Array.isArray(grants) || !grants.length || grants.some((grant) =>
  grant?.platform !== 'telegram' || grant?.accountId !== TELEGRAM_BOT_ID
  || typeof grant?.conversationId !== 'string' || !grant.conversationId
  || typeof grant?.senderId !== 'string' || !grant.senderId)) {
  throw new Error('Provide explicit Telegram bot, conversation and sender grants');
}
const mode = process.env.CHANNEL_KIT_RUNNER ?? 'echo';
if (!['echo', 'codex'].includes(mode)) throw new Error('CHANNEL_KIT_RUNNER must be echo or codex');
const runner = mode === 'codex' ? new CodexCliRunner() : new EchoRunner();
const stateRoot = process.env.XDG_STATE_HOME || join(homedir(), '.local', 'state');
const statePath = process.env.TELEGRAM_STATE_FILE || join(stateRoot, 'channel-kit', `telegram-${TELEGRAM_BOT_ID}.sqlite`);
const store = new TelegramPollStore(statePath, TELEGRAM_BOT_ID);
const host = new TelegramPollingHost({ client: new TelegramBotClient(TELEGRAM_BOT_TOKEN), store, grants, runner });
const stop = new AbortController();
process.once('SIGINT', () => stop.abort()); process.once('SIGTERM', () => stop.abort());
try {
  while (!stop.signal.aborted) {
    const receipts = await host.pollOnce({ signal: stop.signal });
    for (const receipt of receipts) console.log(JSON.stringify(receipt));
  }
} catch {
  if (!stop.signal.aborted) {
    console.error('Telegram polling stopped. Inspect the ledger before retrying any unknown operation.');
    process.exitCode = 1;
  }
} finally { store.close(); }
