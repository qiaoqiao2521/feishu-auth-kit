/** Direct upstream composition spike; not a new messaging protocol or production gateway. */
import { Chat, ConsoleLogger } from 'chat';
import { createTelegramAdapter } from '@chat-adapter/telegram';
import { createMemoryState } from '@chat-adapter/state-memory';

/**
 * One Chat and one fresh memory state per connected bot. Volatile, offline pilot.
 * The response callback receives selected model input, never the SDK raw message.
 */
export function createTelegramPilot({ accountId, botToken, secretToken, userName, grants, respond }) {
  if (typeof accountId !== 'string' || !/^[1-9][0-9]*$/.test(accountId)
    || typeof botToken !== 'string' || !botToken.startsWith(`${accountId}:`)) {
    throw new TypeError('Provide the expected bot account and its token');
  }
  if (typeof secretToken !== 'string' || !/^[A-Za-z0-9_-]{1,256}$/.test(secretToken)) {
    throw new TypeError('A Telegram webhook secret is required');
  }
  if (typeof userName !== 'string' || !userName.trim()) throw new TypeError('Bot userName is required');
  if (!Array.isArray(grants) || !grants.length || grants.some((grant) =>
    typeof grant?.userId !== 'string' || !grant.userId
    || typeof grant?.chatId !== 'string' || !grant.chatId
    || (grant.topicId !== undefined && (!Number.isSafeInteger(grant.topicId) || grant.topicId <= 0)))) {
    throw new TypeError('Provide explicit user and chat grants, optionally scoped to a topic');
  }
  const allowed = grants.map((grant) => Object.freeze({ ...grant }));
  const adapter = createTelegramAdapter({ botToken, secretToken, userName,
    mode: 'webhook', allowedUserIds: [...new Set(allowed.map((grant) => grant.userId))],
    allowUnverifiedWebhooks: false, nativeStreaming: false, logger: new ConsoleLogger('silent') });
  const bot = new Chat({ userName, adapters: { telegram: adapter },
    state: createMemoryState(), logger: 'silent', concurrency: 'concurrent' });

  const handle = async (thread, message) => {
    const target = adapter.decodeThreadId(thread.id);
    if (message.author.isBot || message.author.isMe || target.businessConnectionId
      || !allowed.some((grant) => grant.userId === message.author.userId
        && grant.chatId === target.chatId
        && (grant.topicId === undefined || grant.topicId === target.messageThreadId))) return;
    const input = Object.freeze({ accountId, threadId: thread.id, messageId: message.id,
      senderId: message.author.userId, text: message.text });
    const output = respond ? await respond(input) : `Echo: ${input.text}`;
    if (typeof output === 'string' && output.trim()) await thread.post(output);
  };
  bot.onDirectMessage(handle);
  bot.onNewMention(handle);
  bot.onSubscribedMessage(handle);
  bot.onNewMessage(handle);
  return bot;
}
