/** Configuration for this text-only, single-bot pilot; no Feishu credential reads. */
export function readTelegramIdentity(env = process.env) {
  const botToken = env.TELEGRAM_BOT_TOKEN?.trim();
  const accountId = botToken?.split(':')[0];
  if (!/^[1-9][0-9]*:[A-Za-z0-9_-]+$/.test(botToken ?? '')) {
    throw new Error('TELEGRAM_BOT_TOKEN is required (BotFather token)');
  }
  const secretToken = env.TELEGRAM_WEBHOOK_SECRET_TOKEN;
  if (!/^[A-Za-z0-9_-]{1,256}$/.test(secretToken ?? '')) {
    throw new Error('TELEGRAM_WEBHOOK_SECRET_TOKEN must contain 1–256 letters, digits, _ or -');
  }
  const userName = env.TELEGRAM_BOT_USERNAME?.trim().replace(/^@/, '');
  if (!/^[A-Za-z0-9_]+bot$/i.test(userName ?? '')) {
    throw new Error('TELEGRAM_BOT_USERNAME is required (the bot username ending in bot)');
  }
  return { accountId, botToken, secretToken, userName };
}

export function readTelegramConfig(env = process.env) {
  const identity = readTelegramIdentity(env);
  let grants;
  try { grants = JSON.parse(env.TELEGRAM_GRANTS ?? ''); } catch {
    throw new Error('TELEGRAM_GRANTS must be a JSON array of explicit userId/chatId grants');
  }
  if (!Array.isArray(grants) || !grants.length || grants.some((g) =>
    !g || typeof g.userId !== 'string' || !/^[1-9][0-9]*$/.test(g.userId)
    || typeof g.chatId !== 'string' || !/^-?[1-9][0-9]*$/.test(g.chatId)
    || (g.topicId !== undefined && (!Number.isSafeInteger(g.topicId) || g.topicId <= 0)))) {
    throw new Error('TELEGRAM_GRANTS requires numeric string userId/chatId and optional positive integer topicId');
  }
  const port = env.TELEGRAM_PORT ?? '8787';
  if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
    throw new Error('TELEGRAM_PORT must be 1–65535');
  }
  return { ...identity, grants,
    host: env.TELEGRAM_HOST?.trim() || '127.0.0.1', port: Number(port) };
}

export function permits(grants, message) {
  return message.from?.is_bot === false && grants.some((g) =>
    g.userId === String(message.from.id) && g.chatId === String(message.chat.id)
    && (g.topicId === undefined || g.topicId === message.message_thread_id));
}
