import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  markRetryDelivered,
  markRetryFailed,
  NativeCardAction,
  NativeContinuationRecord,
  resolveCardActionToRetry,
  saveNativeContinuation,
} from '../src/native-contract.js';
import { FileContinuationStore } from '../src/runtime-cards.js';

function makeStore(): { store: FileContinuationStore; cleanup: () => void } {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'contract-idem-'));
  return {
    store: new FileContinuationStore(path.join(tmpDir, 'continuations.json')),
    cleanup: () => fs.rmSync(tmpDir, { recursive: true, force: true }),
  };
}

function makeContinuation(operationId: string): NativeContinuationRecord {
  return new NativeContinuationRecord({
    operationId,
    appId: 'cli_xxx',
    continuationKind: 'plan_confirmation',
    retryText: '继续执行计划',
    userOpenId: 'ou_user',
    requiredScopes: [],
    metadata: { session_id: 'sess-1' },
  });
}

describe('native-contract idempotency and delivery state machine', () => {
  it('suppresses duplicate clicks and returns the recorded resolution', () => {
    const { store, cleanup } = makeStore();
    try {
      saveNativeContinuation(store, makeContinuation('op-dup'));

      const first = resolveCardActionToRetry(
        new NativeCardAction({
          operationId: 'op-dup',
          action: 'plan_approve',
          actorOpenId: 'ou_actor',
          payload: { operation_id: 'op-dup' },
        }),
        store,
      );
      expect(first.duplicate).toBe(false);
      expect(first.continuation.status).toBe('confirmed');

      const second = resolveCardActionToRetry(
        new NativeCardAction({
          operationId: 'op-dup',
          action: 'plan_approve',
          actorOpenId: 'ou_actor',
          payload: { operation_id: 'op-dup' },
        }),
        store,
      );
      expect(second.duplicate).toBe(true);
      expect(second.retryRequest.action).toBe(first.retryRequest.action);
      expect(second.retryRequest.text).toBe(first.retryRequest.text);
      expect(second.retryRequest.actorOpenId).toBe('ou_actor');
      expect(second.continuation.status).toBe('confirmed');
    } finally {
      cleanup();
    }
  });

  it('locks the decision against a different action after confirmation', () => {
    const { store, cleanup } = makeStore();
    try {
      saveNativeContinuation(store, makeContinuation('op-lock'));

      resolveCardActionToRetry(
        new NativeCardAction({ operationId: 'op-lock', action: 'plan_approve' }),
        store,
      );
      const rejected = resolveCardActionToRetry(
        new NativeCardAction({ operationId: 'op-lock', action: 'plan_reject' }),
        store,
      );
      expect(rejected.duplicate).toBe(true);
      expect(rejected.retryRequest.action).toBe('plan_approve');
      expect(rejected.continuation.status).toBe('confirmed');
    } finally {
      cleanup();
    }
  });

  it('marks delivery: confirmed -> delivered, duplicates still suppressed', () => {
    const { store, cleanup } = makeStore();
    try {
      saveNativeContinuation(store, makeContinuation('op-deliver'));
      resolveCardActionToRetry(
        new NativeCardAction({ operationId: 'op-deliver', action: 'plan_approve' }),
        store,
      );

      const delivered = markRetryDelivered(store, 'op-deliver');
      expect(delivered.status).toBe('delivered');

      const again = markRetryDelivered(store, 'op-deliver');
      expect(again.status).toBe('delivered');

      const late = resolveCardActionToRetry(
        new NativeCardAction({ operationId: 'op-deliver', action: 'plan_approve' }),
        store,
      );
      expect(late.duplicate).toBe(true);
      expect(late.continuation.status).toBe('delivered');
    } finally {
      cleanup();
    }
  });

  it('recovers from delivery failure: failed -> waiting -> fresh re-resolve', () => {
    const { store, cleanup } = makeStore();
    try {
      saveNativeContinuation(store, makeContinuation('op-fail'));
      resolveCardActionToRetry(
        new NativeCardAction({
          operationId: 'op-fail',
          action: 'plan_approve',
          actorOpenId: 'ou_first',
        }),
        store,
      );

      const reset = markRetryFailed(store, 'op-fail', { reason: 'delivery timeout' });
      expect(reset.status).toBe('waiting');
      expect(reset.metadata.delivery_failures).toBe(1);
      expect(reset.metadata.last_failure_reason).toBe('delivery timeout');
      expect(reset.metadata.last_confirmed_action).toBe('plan_approve');
      expect(reset.metadata.resolved_action).toBeUndefined();
      expect(reset.metadata.resolved_retry_request).toBeUndefined();

      const retried = resolveCardActionToRetry(
        new NativeCardAction({
          operationId: 'op-fail',
          action: 'plan_approve',
          actorOpenId: 'ou_second',
        }),
        store,
      );
      expect(retried.duplicate).toBe(false);
      expect(retried.retryRequest.actorOpenId).toBe('ou_second');
      expect(retried.continuation.status).toBe('confirmed');
      expect(retried.continuation.metadata.delivery_failures).toBe(1);
    } finally {
      cleanup();
    }
  });

  it('rejects invalid transitions and unknown operations', () => {
    const { store, cleanup } = makeStore();
    try {
      saveNativeContinuation(store, makeContinuation('op-bad'));
      expect(() => markRetryDelivered(store, 'op-bad')).toThrow(/waiting/);
      expect(() => markRetryFailed(store, 'op-bad')).toThrow(/waiting/);
      expect(() => markRetryDelivered(store, 'op-missing')).toThrow(/unknown/);
      expect(() => markRetryFailed(store, 'op-missing')).toThrow(/unknown/);
    } finally {
      cleanup();
    }
  });

  it('toDict exposes the duplicate flag', () => {
    const { store, cleanup } = makeStore();
    try {
      saveNativeContinuation(store, makeContinuation('op-dict'));
      const first = resolveCardActionToRetry(
        new NativeCardAction({ operationId: 'op-dict', action: 'plan_approve' }),
        store,
      );
      const second = resolveCardActionToRetry(
        new NativeCardAction({ operationId: 'op-dict', action: 'plan_approve' }),
        store,
      );
      expect(first.toDict().duplicate).toBe(false);
      expect(second.toDict().duplicate).toBe(true);
    } finally {
      cleanup();
    }
  });
});
