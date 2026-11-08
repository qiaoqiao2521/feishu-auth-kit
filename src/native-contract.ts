import {
  AuthContinuation,
  SyntheticRetryArtifact,
  buildSyntheticRetryArtifact,
  loadAuthContinuation,
} from './orchestration.js';
import { ContinuationState, FileContinuationStore } from './runtime-cards.js';

export const NATIVE_CONTINUATION_KIND = 'native_agent_continuation';

export const NATIVE_CONTINUATION_STATUS = {
  waiting: 'waiting',
  confirmed: 'confirmed',
  delivered: 'delivered',
} as const;

export class NativeCardAction {
  readonly operationId: string;
  readonly action: string;
  readonly actorOpenId: string | null;
  readonly messageId: string | null;
  readonly payload: Record<string, any>;

  constructor(options: {
    operationId: string;
    action: string;
    actorOpenId?: string | null;
    messageId?: string | null;
    payload?: Record<string, any>;
  }) {
    this.operationId = options.operationId;
    this.action = options.action;
    this.actorOpenId = options.actorOpenId ?? null;
    this.messageId = options.messageId ?? null;
    this.payload = options.payload ? { ...options.payload } : {};
  }

  toDict(): Record<string, any> {
    return {
      schema: 'feishu-auth-kit.native-card-action.v1',
      operation_id: this.operationId,
      action: this.action,
      actor_open_id: this.actorOpenId,
      message_id: this.messageId,
      payload: this.payload,
    };
  }
}

export class NativeContinuationRecord {
  readonly operationId: string;
  readonly appId: string;
  readonly continuationKind: string;
  readonly retryText: string;
  readonly flowKey: string | null;
  readonly userOpenId: string | null;
  readonly requiredScopes: string[];
  readonly status: string;
  readonly metadata: Record<string, any>;

  constructor(options: {
    operationId: string;
    appId: string;
    continuationKind: string;
    retryText: string;
    flowKey?: string | null;
    userOpenId?: string | null;
    requiredScopes?: string[];
    status?: string;
    metadata?: Record<string, any>;
  }) {
    this.operationId = options.operationId;
    this.appId = options.appId;
    this.continuationKind = options.continuationKind;
    this.retryText = options.retryText;
    this.flowKey = options.flowKey ?? null;
    this.userOpenId = options.userOpenId ?? null;
    this.requiredScopes = options.requiredScopes ? [...options.requiredScopes] : [];
    this.status = options.status ?? 'waiting';
    this.metadata = options.metadata ? { ...options.metadata } : {};
  }

  static fromAuthContinuation(
    continuation: AuthContinuation,
    options: {
      retryText: string;
      status?: string;
      metadata?: Record<string, any>;
    },
  ): NativeContinuationRecord {
    return new NativeContinuationRecord({
      operationId: continuation.operationId,
      appId: continuation.appId,
      continuationKind: continuation.decision,
      retryText: options.retryText,
      flowKey: continuation.flowKey,
      userOpenId: continuation.userOpenId,
      requiredScopes: [...continuation.requiredScopes],
      status: options.status ?? 'waiting',
      metadata: { ...continuation.metadata, ...(options.metadata || {}) },
    });
  }

  toState(): ContinuationState {
    const payload = {
      flow_key: this.flowKey,
      user_open_id: this.userOpenId,
      required_scopes: this.requiredScopes,
      retry_text: this.retryText,
      continuation_kind: this.continuationKind,
      metadata: this.metadata,
    };
    return {
      operationId: this.operationId,
      appId: this.appId,
      kind: NATIVE_CONTINUATION_KIND,
      status: this.status,
      payload,
    };
  }

  static fromState(state: ContinuationState): NativeContinuationRecord {
    const payload = state.payload;
    return new NativeContinuationRecord({
      operationId: state.operationId,
      appId: state.appId,
      continuationKind: String(payload.continuation_kind),
      retryText: String(payload.retry_text),
      flowKey: payload.flow_key ?? null,
      userOpenId: payload.user_open_id ?? null,
      requiredScopes: Array.isArray(payload.required_scopes) ? payload.required_scopes : [],
      status: state.status,
      metadata: payload.metadata || {},
    });
  }

  toDict(): Record<string, any> {
    return {
      schema: 'feishu-auth-kit.native-continuation.v1',
      operation_id: this.operationId,
      app_id: this.appId,
      continuation_kind: this.continuationKind,
      retry_text: this.retryText,
      flow_key: this.flowKey,
      user_open_id: this.userOpenId,
      required_scopes: this.requiredScopes,
      status: this.status,
      metadata: this.metadata,
    };
  }
}

export class NativeRetryRequest {
  readonly operationId: string;
  readonly appId: string;
  readonly continuationKind: string;
  readonly action: string;
  readonly text: string;
  readonly actorOpenId: string | null;
  readonly userOpenId: string | null;
  readonly metadata: Record<string, any>;

  constructor(options: {
    operationId: string;
    appId: string;
    continuationKind: string;
    action: string;
    text: string;
    actorOpenId?: string | null;
    userOpenId?: string | null;
    metadata?: Record<string, any>;
  }) {
    this.operationId = options.operationId;
    this.appId = options.appId;
    this.continuationKind = options.continuationKind;
    this.action = options.action;
    this.text = options.text;
    this.actorOpenId = options.actorOpenId ?? null;
    this.userOpenId = options.userOpenId ?? null;
    this.metadata = options.metadata ? { ...options.metadata } : {};
  }

  toDict(): Record<string, any> {
    return {
      schema: 'feishu-auth-kit.native-retry-request.v1',
      operation_id: this.operationId,
      app_id: this.appId,
      continuation_kind: this.continuationKind,
      action: this.action,
      text: this.text,
      actor_open_id: this.actorOpenId,
      user_open_id: this.userOpenId,
      metadata: this.metadata,
    };
  }
}

export class ResolvedNativeAction {
  readonly action: NativeCardAction;
  readonly continuation: NativeContinuationRecord;
  readonly retryRequest: NativeRetryRequest;
  /** True when the click was suppressed because this operation was already confirmed/delivered. */
  readonly duplicate: boolean;

  constructor(options: {
    action: NativeCardAction;
    continuation: NativeContinuationRecord;
    retryRequest: NativeRetryRequest;
    duplicate?: boolean;
  }) {
    this.action = options.action;
    this.continuation = options.continuation;
    this.retryRequest = options.retryRequest;
    this.duplicate = options.duplicate ?? false;
  }

  toDict(): Record<string, any> {
    return {
      schema: 'feishu-auth-kit.native-action-resolution.v1',
      duplicate: this.duplicate,
      action: this.action.toDict(),
      continuation: this.continuation.toDict(),
      retry_request: this.retryRequest.toDict(),
    };
  }
}

interface StoredResolution {
  action: NativeCardAction;
  retryRequest: NativeRetryRequest;
}

function storedResolution(continuation: NativeContinuationRecord): StoredResolution | null {
  const action = continuation.metadata.resolved_action;
  const retryRequest = continuation.metadata.resolved_retry_request;
  if (!action || !retryRequest) {
    return null;
  }
  return {
    action: new NativeCardAction({
      operationId: String(action.operation_id),
      action: String(action.action),
      actorOpenId: action.actor_open_id ?? null,
      messageId: action.message_id ?? null,
      payload: action.payload || {},
    }),
    retryRequest: new NativeRetryRequest({
      operationId: String(retryRequest.operation_id),
      appId: String(retryRequest.app_id),
      continuationKind: String(retryRequest.continuation_kind),
      action: String(retryRequest.action),
      text: String(retryRequest.text),
      actorOpenId: retryRequest.actor_open_id ?? null,
      userOpenId: retryRequest.user_open_id ?? null,
      metadata: retryRequest.metadata || {},
    }),
  };
}

export function saveNativeContinuation(
  store: FileContinuationStore,
  continuation: NativeContinuationRecord,
): ContinuationState {
  const state = continuation.toState();
  store.save(state);
  return state;
}

export function loadNativeContinuation(
  store: FileContinuationStore,
  operationId: string,
): NativeContinuationRecord | null {
  const state = store.load(operationId);
  if (!state || state.kind !== NATIVE_CONTINUATION_KIND) {
    return null;
  }
  return NativeContinuationRecord.fromState(state);
}

export function bindAuthContinuationToNative(
  store: FileContinuationStore,
  options: {
    operationId: string;
    retryText: string;
    metadata?: Record<string, any>;
  },
): NativeContinuationRecord {
  const current = loadNativeContinuation(store, options.operationId);
  if (current) {
    const updated = new NativeContinuationRecord({
      operationId: current.operationId,
      appId: current.appId,
      continuationKind: current.continuationKind,
      retryText: options.retryText,
      flowKey: current.flowKey,
      userOpenId: current.userOpenId,
      requiredScopes: current.requiredScopes,
      status: current.status,
      metadata: { ...current.metadata, ...(options.metadata || {}) },
    });
    saveNativeContinuation(store, updated);
    return updated;
  }

  const continuation = loadAuthContinuation(store, options.operationId);
  if (!continuation) {
    throw new Error(`unknown continuation operation_id: ${options.operationId}`);
  }
  const native = NativeContinuationRecord.fromAuthContinuation(continuation, {
    retryText: options.retryText,
    metadata: options.metadata,
  });
  saveNativeContinuation(store, native);
  return native;
}

export function resolveCardActionToRetry(
  action: NativeCardAction,
  store: FileContinuationStore,
): ResolvedNativeAction {
  const continuation = loadNativeContinuation(store, action.operationId);
  if (!continuation) {
    throw new Error(`unknown native continuation operation_id: ${action.operationId}`);
  }

  // Duplicate-click guard: once a click is confirmed (or delivered) the decision
  // is locked. Later clicks — double clicks, concurrent callbacks, replays after a
  // slow transport — return the recorded resolution with duplicate=true and never
  // emit a second retry request, so the card cannot drift into a dirty state.
  if (continuation.status === 'confirmed' || continuation.status === 'delivered') {
    const stored = storedResolution(continuation);
    if (stored) {
      return new ResolvedNativeAction({
        action: stored.action,
        continuation,
        retryRequest: stored.retryRequest,
        duplicate: true,
      });
    }
    // confirmed/delivered without a stored resolution (e.g. hand-edited state):
    // still refuse to double-confirm, but surface the current continuation.
    return new ResolvedNativeAction({
      action,
      continuation,
      retryRequest: new NativeRetryRequest({
        operationId: continuation.operationId,
        appId: continuation.appId,
        continuationKind: continuation.continuationKind,
        action: action.action,
        text: continuation.retryText,
        actorOpenId: action.actorOpenId,
        userOpenId: continuation.userOpenId,
        metadata: continuation.metadata,
      }),
      duplicate: true,
    });
  }

  const confirmedMetadata = {
    ...continuation.metadata,
    last_action: action.action,
    actor_open_id: action.actorOpenId,
    action_payload: action.payload,
    message_id: action.messageId,
  };
  const retryRequest = new NativeRetryRequest({
    operationId: continuation.operationId,
    appId: continuation.appId,
    continuationKind: continuation.continuationKind,
    action: action.action,
    text: continuation.retryText,
    actorOpenId: action.actorOpenId,
    userOpenId: continuation.userOpenId,
    metadata: confirmedMetadata,
  });
  const confirmed = new NativeContinuationRecord({
    operationId: continuation.operationId,
    appId: continuation.appId,
    continuationKind: continuation.continuationKind,
    retryText: continuation.retryText,
    flowKey: continuation.flowKey,
    userOpenId: continuation.userOpenId,
    requiredScopes: continuation.requiredScopes,
    status: 'confirmed',
    metadata: {
      ...confirmedMetadata,
      resolved_action: action.toDict(),
      resolved_retry_request: retryRequest.toDict(),
    },
  });
  saveNativeContinuation(store, confirmed);

  return new ResolvedNativeAction({
    action,
    continuation: confirmed,
    retryRequest,
  });
}

export function markRetryDelivered(
  store: FileContinuationStore,
  operationId: string,
): NativeContinuationRecord {
  const continuation = loadNativeContinuation(store, operationId);
  if (!continuation) {
    throw new Error(`unknown native continuation operation_id: ${operationId}`);
  }
  if (continuation.status === 'delivered') {
    return continuation;
  }
  if (continuation.status !== 'confirmed') {
    throw new Error(
      `cannot mark retry delivered for operation_id ${operationId} in status ${continuation.status}`,
    );
  }
  const delivered = new NativeContinuationRecord({
    operationId: continuation.operationId,
    appId: continuation.appId,
    continuationKind: continuation.continuationKind,
    retryText: continuation.retryText,
    flowKey: continuation.flowKey,
    userOpenId: continuation.userOpenId,
    requiredScopes: continuation.requiredScopes,
    status: 'delivered',
    metadata: continuation.metadata,
  });
  saveNativeContinuation(store, delivered);
  return delivered;
}

export function markRetryFailed(
  store: FileContinuationStore,
  operationId: string,
  options?: { reason?: string | null },
): NativeContinuationRecord {
  const continuation = loadNativeContinuation(store, operationId);
  if (!continuation) {
    throw new Error(`unknown native continuation operation_id: ${operationId}`);
  }
  if (continuation.status !== 'confirmed' && continuation.status !== 'delivered') {
    throw new Error(
      `cannot mark retry failed for operation_id ${operationId} in status ${continuation.status}`,
    );
  }
  const failures = Number(continuation.metadata.delivery_failures ?? 0) + 1;
  const metadata = { ...continuation.metadata };
  delete metadata.resolved_action;
  delete metadata.resolved_retry_request;
  // Back to waiting: the next genuine click re-resolves from scratch, so a timed
  // out or errored delivery never wedges the card in a half-applied state.
  const reset = new NativeContinuationRecord({
    operationId: continuation.operationId,
    appId: continuation.appId,
    continuationKind: continuation.continuationKind,
    retryText: continuation.retryText,
    flowKey: continuation.flowKey,
    userOpenId: continuation.userOpenId,
    requiredScopes: continuation.requiredScopes,
    status: 'waiting',
    metadata: {
      ...metadata,
      delivery_failures: failures,
      last_failure_reason: options?.reason ?? null,
      last_confirmed_action: continuation.metadata.last_action ?? null,
    },
  });
  saveNativeContinuation(store, reset);
  return reset;
}

export function buildRetryArtifactFromRequest(
  request: NativeRetryRequest,
): SyntheticRetryArtifact {
  return buildSyntheticRetryArtifact({
    operationId: request.operationId,
    appId: request.appId,
    userOpenId: request.userOpenId,
    text: request.text,
    reason: request.action,
    metadata: { actor_open_id: request.actorOpenId, ...request.metadata },
  });
}
