import { describe, expect, it } from 'vitest';
import { authorizeMessage, type AuthorizationGrant } from '../src/core/policy.js';

const subject = {
  platform: 'telegram', accountId: 'bot-a', conversationId: 'chat-a',
  senderId: 'user-a', threadId: 'thread-a',
};

const principal = {
  platform: subject.platform, accountId: subject.accountId, senderId: subject.senderId,
};

describe('channel authorization', () => {
  it('denies without an explicit grant', () => {
    expect(authorizeMessage(subject)).toEqual({ allowed: false, reason: 'no_matching_grant' });
  });

  it.each(['platform', 'accountId', 'senderId'] as const)(
    'does not reuse a matching ID across another %s', (field) => {
      expect(authorizeMessage({ ...subject, [field]: 'other' }, [principal]).allowed).toBe(false);
      expect(authorizeMessage(subject, [principal]).allowed).toBe(true);
    },
  );

  it('enforces exact conversation and thread scopes', () => {
    const grant = { ...principal, conversationId: 'chat-a', threadId: 'thread-a' };
    expect(authorizeMessage(subject, [grant]).allowed).toBe(true);
    expect(authorizeMessage({ ...subject, conversationId: 'chat-b' }, [grant]).allowed).toBe(false);
    expect(authorizeMessage({ ...subject, threadId: 'thread-b' }, [grant]).allowed).toBe(false);
    expect(authorizeMessage({ ...subject, threadId: undefined }, [grant]).allowed).toBe(false);
  });

  it('allows every thread only when its scope is omitted', () => {
    const grant = { ...principal, conversationId: 'chat-a' };
    expect(authorizeMessage(subject, [grant]).allowed).toBe(true);
    expect(authorizeMessage({ ...subject, threadId: 'thread-b' }, [grant]).allowed).toBe(true);
    expect(authorizeMessage({ ...subject, threadId: undefined }, [grant]).allowed).toBe(true);
    expect(authorizeMessage({ ...subject, conversationId: 'chat-b' }, [grant]).allowed).toBe(false);
  });

  it('rejects a thread grant without its conversation scope', () => {
    expect(authorizeMessage(subject, [{ ...principal, threadId: 'thread-a' }]).allowed).toBe(false);
  });

  it('treats stars literally instead of as wildcard permission', () => {
    const grant = { platform: '*', accountId: '*', senderId: '*' };
    expect(authorizeMessage(subject, [grant]).allowed).toBe(false);
    expect(authorizeMessage({ ...subject, ...grant }, [grant]).allowed).toBe(true);
    expect(authorizeMessage(subject, [{ ...principal, conversationId: '*' }]).allowed).toBe(false);
  });

  it.each(['platform', 'accountId', 'senderId', 'conversationId', 'threadId'] as const)(
    'fails closed on an empty inbound %s', (field) => {
      expect(authorizeMessage({ ...subject, [field]: ' ' }, [principal]))
        .toEqual({ allowed: false, reason: 'invalid_identity' });
    },
  );

  it.each(['platform', 'accountId', 'senderId', 'conversationId', 'threadId'] as const)(
    'ignores an invalid empty grant %s', (field) => {
      const invalid = { ...principal, conversationId: 'chat-a', threadId: 'thread-a', [field]: '' };
      expect(authorizeMessage(subject, [invalid]).allowed).toBe(false);
    },
  );

  it('fails closed for malformed runtime identities', () => {
    expect(authorizeMessage(null as never, [principal]).reason).toBe('invalid_identity');
    expect(authorizeMessage(subject, [null as unknown as AuthorizationGrant]).allowed).toBe(false);
    expect(authorizeMessage(subject, [{ ...principal, accountId: 42 } as never]).allowed).toBe(false);
  });
});
