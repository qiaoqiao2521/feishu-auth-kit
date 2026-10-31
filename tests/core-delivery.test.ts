import { describe, expect, it } from 'vitest';
import {
  DEFAULT_CHANNEL_CAPABILITIES, canRetryDelivery, isFinalDelivery, type DeliveryOutcome,
} from '../src/core/delivery.js';

describe('channel delivery contracts', () => {
  it('requires adapters to opt into features beyond text', () => {
    expect(DEFAULT_CHANNEL_CAPABILITIES).toEqual({
      text: true, preview: false, edit: false, attachments: false, threads: false, reactions: false,
    });
    expect(Object.isFrozen(DEFAULT_CHANNEL_CAPABILITIES)).toBe(true);
  });

  it('does not treat a delivered preview as a final reply', () => {
    expect(isFinalDelivery({ status: 'delivered', phase: 'preview', messageId: 'preview-1' })).toBe(false);
    expect(isFinalDelivery({ status: 'delivered', phase: 'final', messageId: 'reply-1' })).toBe(true);
    expect(isFinalDelivery({ status: 'delivered', phase: 'final', messageId: '' })).toBe(false);
  });

  it.each(['preview', 'final'] as const)('does not infer delivery from a failed or unknown %s', (phase) => {
    expect(isFinalDelivery({ status: 'failed', phase, reason: 'rejected' })).toBe(false);
    expect(isFinalDelivery({ status: 'unknown', phase, reason: 'connection lost' })).toBe(false);
  });

  it.each(['preview', 'final'] as const)('permits explicit retry only for a confirmed unsent %s', (phase) => {
    const outcomes: DeliveryOutcome[] = [
      { status: 'delivered', phase, messageId: 'sent-1' },
      { status: 'failed', phase, reason: 'local validation failed' },
      { status: 'unknown', phase, reason: 'acknowledgement lost', operationId: 'op-1' },
    ];
    expect(outcomes.map(canRetryDelivery)).toEqual([false, true, false]);
  });
});
