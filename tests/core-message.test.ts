import { describe, expect, it } from 'vitest';
import { ChannelEnvelope, ChannelMessage, buildSessionKey } from '../src/core/index.js';
import { adaptFeishuMessage } from '../src/feishu-message-adapter.js';
import { FeishuMessageContext } from '../src/message-context.js';

function message(overrides: Partial<ConstructorParameters<typeof ChannelMessage>[0]> = {}) {
  return new ChannelMessage({
    platform: 'telegram', accountId: 'bot-1', conversationId: 'chat-1',
    conversationType: 'group', senderId: 'user-1', messageId: 'message-1',
    text: '  Keep\n  indentation.  ', ...overrides,
  });
}

describe('ChannelMessage and transport envelope', () => {
  it('serializes only normalized public fields and never private reply context', () => {
    const input = {
      platform: 'telegram', accountId: 'bot', conversationId: 'chat',
      conversationType: 'direct' as const, senderId: 'user', messageId: 'message', text: 'Hello',
      raw: { token: 'raw-secret' }, botToken: 'bot-secret',
      attachments: [{ kind: 'file' as const, id: 'opaque-file', name: 'notes.txt',
        mimeType: 'text/plain', sizeBytes: 12, url: 'https://signed.test/token-secret', token: 'attachment-secret' }],
    };
    const context = { token: 'reply-secret', raw: input };
    const envelope = new ChannelEnvelope(new ChannelMessage(input), context);

    expect(envelope.getReplyContext()).toBe(context);
    expect(Object.keys(envelope)).toEqual(['message']);
    expect(envelope.toDict()).toEqual({
      schema: 'channel-kit.message.v1', platform: 'telegram', account_id: 'bot',
      conversation_id: 'chat', conversation_type: 'direct', sender_id: 'user',
      message_id: 'message', text: 'Hello',
      attachments: [{ kind: 'file', id: 'opaque-file', name: 'notes.txt', mime_type: 'text/plain', size_bytes: 12 }],
    });
    expect(JSON.stringify(envelope)).not.toContain('secret');
    expect(JSON.stringify(envelope.message)).not.toContain('signed.test');
  });

  it('detaches input attachments and preserves multiline prompt text', () => {
    const attachment = { kind: 'file' as const, id: 'file-1' };
    const attachments = [attachment];
    const normalized = message({ attachments });
    attachment.id = 'file-2';
    attachments.push({ kind: 'file', id: 'file-3' });
    expect(normalized.attachments).toEqual([{ kind: 'file', id: 'file-1' }]);
    expect(normalized.promptText()).toBe('Keep\n  indentation.');
  });

  it.each(['platform', 'accountId', 'conversationId', 'senderId', 'messageId', 'threadId', 'replyToMessageId'])(
    'rejects missing identity in %s', (field) => {
      expect(() => message({ [field]: '' })).toThrow(TypeError);
    },
  );

  it('adapts Feishu without altering the legacy message or exposing its raw event', () => {
    const context = new FeishuMessageContext({
      app_id: 'cli-app', chat_id: 'oc-chat', chat_type: 'p2p',
      sender_open_id: 'ou-owner', message_id: 'om-message',
      text: '@_user_1 hello', raw: { token: 'feishu-private-token' },
    });
    const envelope = adaptFeishuMessage(context);
    expect(envelope.message.platform).toBe('feishu');
    expect(envelope.message.accountId).toBe('cli-app');
    expect(envelope.message.conversationType).toBe('direct');
    expect(envelope.message.promptText()).toBe('hello');
    expect(envelope.getReplyContext()).toBe(context);
    expect(context.raw).toEqual({ token: 'feishu-private-token' });
    expect(JSON.stringify(envelope)).not.toContain('feishu-private-token');
  });

  it('requires Feishu account and actor identities instead of inventing shared scopes', () => {
    expect(() => adaptFeishuMessage(new FeishuMessageContext({ text: 'hello' }))).toThrow(TypeError);
  });

  it('preserves Feishu thread and reply scope while retaining other provider fields privately', () => {
    const context = new FeishuMessageContext({
      app_id: 'app', chat_id: 'chat', chat_type: 'group', sender_open_id: 'sender', message_id: 'message',
      raw: { event: { message: { thread_id: 'topic', root_id: 'root', parent_id: 'parent', token: 'private-secret' } } },
    });
    const normalized = adaptFeishuMessage(context).message;
    expect(normalized.threadId).toBe('topic');
    expect(normalized.replyToMessageId).toBe('parent');
    expect(JSON.stringify(normalized)).not.toContain('private-secret');
    const rootThread = adaptFeishuMessage(new FeishuMessageContext({
      app_id: 'app', chat_id: 'chat', chat_type: 'group', sender_open_id: 'sender', message_id: 'message',
      raw: { message: { root_id: 'root', parent_id: 'parent' } },
    })).message;
    expect(rootThread.threadId).toBe('root');
    expect(buildSessionKey(rootThread)).not.toBe(buildSessionKey(normalized));
  });
});

describe('channel session isolation', () => {
  it.each([
    { platform: 'feishu' }, { accountId: 'bot-2' }, { conversationId: 'chat-2' },
    { threadId: 'thread-1' }, { senderId: 'user-2' },
  ])('isolates the scope %j', (override) => {
    expect(buildSessionKey(message(override))).not.toBe(buildSessionKey(message()));
  });

  it('does not partition one conversation by each message or reply ID', () => {
    expect(buildSessionKey(message({ messageId: 'message-2', replyToMessageId: 'message-1' })))
      .toBe(buildSessionKey(message()));
  });

  it('shares senders only on explicit request while keeping thread isolation', () => {
    const shared = { includeSender: false };
    expect(buildSessionKey(message(), shared)).toBe(buildSessionKey(message({ senderId: 'user-2' }), shared));
    expect(buildSessionKey(message(), shared)).not.toBe(buildSessionKey(message({ threadId: 'topic-1' }), shared));
    expect(buildSessionKey(message(), shared)).not.toBe(buildSessionKey(message()));
  });

  it('avoids delimiter collisions in identifiers', () => {
    expect(buildSessionKey(message({ accountId: 'a|b', conversationId: 'c' })))
      .not.toBe(buildSessionKey(message({ accountId: 'a', conversationId: 'b|c' })));
  });
});
