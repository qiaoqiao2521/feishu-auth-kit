import type { ChannelMessage } from './message.js';

/** Provider identities are scoped to the connected account, never globally. */
export interface ChannelIdentity {
  readonly platform: string;
  readonly accountId: string;
  readonly senderId: string;
}

/**
 * An exact principal grant, optionally limited to a conversation or thread.
 * An omitted conversation or thread scope permits any value for that scope.
 * A thread scope requires a conversation scope because thread IDs may be local
 * to their conversation. Strings such as "*" have no wildcard meaning.
 */
export interface AuthorizationGrant extends ChannelIdentity {
  readonly conversationId?: string;
  readonly threadId?: string;
}

export interface AuthorizationDecision {
  readonly allowed: boolean;
  readonly reason: 'allowed' | 'no_matching_grant' | 'invalid_identity';
}

type AuthorizationSubject = Pick<ChannelMessage,
  'platform' | 'accountId' | 'senderId' | 'conversationId' | 'threadId'>;

function nonemptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

function validIdentity(value: ChannelIdentity): boolean {
  return value != null
    && nonemptyString(value.platform)
    && nonemptyString(value.accountId)
    && nonemptyString(value.senderId);
}

/**
 * Default-deny authorization for an inbound message. This does not authenticate
 * provider webhooks; adapters must establish the sender identity before calling.
 */
export function authorizeMessage(
  message: AuthorizationSubject,
  grants: readonly AuthorizationGrant[] = [],
): AuthorizationDecision {
  if (!validIdentity(message) || !nonemptyString(message.conversationId)
    || (message.threadId !== undefined && !nonemptyString(message.threadId))) {
    return { allowed: false, reason: 'invalid_identity' };
  }

  const allowed = grants.some((grant) => {
    if (!validIdentity(grant)) return false;
    if (grant.conversationId !== undefined && !nonemptyString(grant.conversationId)) return false;
    if (grant.threadId !== undefined
      && (!nonemptyString(grant.threadId) || !nonemptyString(grant.conversationId))) return false;

    return grant.platform === message.platform
      && grant.accountId === message.accountId
      && grant.senderId === message.senderId
      && (grant.conversationId === undefined || grant.conversationId === message.conversationId)
      && (grant.threadId === undefined || grant.threadId === message.threadId);
  });
  return { allowed, reason: allowed ? 'allowed' : 'no_matching_grant' };
}
