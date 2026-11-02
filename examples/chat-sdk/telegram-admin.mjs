/** Operator setup only. Message delivery remains in the official Chat SDK adapter. */
import { pathToFileURL } from 'node:url';
import { readTelegramConfig, readTelegramIdentity } from './telegram-config.mjs';

export async function botApi(config, method, body = {}, apiUrl = 'https://api.telegram.org') {
  try {
    const response = await fetch(`${apiUrl}/bot${config.botToken}/${method}`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body), signal: AbortSignal.timeout(10_000),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error('provider_rejected');
    return data.result;
  } catch {
    // Never print native fetch errors: their URL contains the bot credential.
    throw new Error(`Telegram ${method} failed; check token, network, webhook conflict or rate limits`);
  }
}

export async function verifyTelegramIdentity(config, apiUrl) {
  const me = await botApi(config, 'getMe', {}, apiUrl);
  if (String(me?.id) !== config.accountId || me?.is_bot !== true
    || me?.username?.toLowerCase() !== config.userName.toLowerCase()) {
    throw new Error('Telegram bot identity does not match the configured account/username');
  }
  return me;
}

export async function runTelegramAdmin(action, env = process.env, { apiUrl, log = console.log } = {}) {
  if (!['check', 'ids', 'register'].includes(action)) {
    throw new Error('Usage: telegram-admin.mjs check|ids|register');
  }
  const config = action === 'register' ? readTelegramConfig(env) : readTelegramIdentity(env);
  const call = (method, body) => botApi(config, method, body, apiUrl);
  await verifyTelegramIdentity(config, apiUrl);
  const info = await call('getWebhookInfo');
  if (action === 'check') {
    log(JSON.stringify({ identity: 'verified', webhookRegistered: Boolean(info.url),
      pendingUpdates: info.pending_update_count, lastErrorPresent: Boolean(info.last_error_date) }));
  } else if (action === 'ids') {
    if (info.url) throw new Error('ids requires an unused bot without a webhook; this command will not remove it');
    const updates = await call('getUpdates', { timeout: 0, limit: 100, allowed_updates: ['message'] });
    log(JSON.stringify(updates.filter((u) => u.message?.from && u.message?.chat).map((u) => ({
      userId: String(u.message.from.id), chatId: String(u.message.chat.id),
      ...(u.message.message_thread_id ? { topicId: u.message.message_thread_id } : {}),
    }))));
  } else {
    let url;
    try { url = new URL(env.TELEGRAM_WEBHOOK_URL); } catch {
      throw new Error('TELEGRAM_WEBHOOK_URL must be a public HTTPS URL ending in /webhooks/telegram');
    }
    if (url.protocol !== 'https:' || url.pathname !== '/webhooks/telegram'
      || url.username || url.password || url.search || url.hash) {
      throw new Error('TELEGRAM_WEBHOOK_URL must be HTTPS with path /webhooks/telegram and no credentials/query');
    }
    if (info.url && info.url !== url.href) {
      throw new Error('A different webhook is already registered; use a separate pilot bot');
    }
    await call('setWebhook', { url: url.href, secret_token: config.secretToken,
      allowed_updates: ['message'], max_connections: 1, drop_pending_updates: false });
    log(JSON.stringify({ webhookRegistered: true, realRoundTrip: 'unverified' }));
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  runTelegramAdmin(process.argv[2]).catch((error) => { console.error(error.message); process.exitCode = 1; });
}
