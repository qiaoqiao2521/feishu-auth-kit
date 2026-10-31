/** Adapter-advertised capabilities; absence of a feature must not imply support. */
export interface ChannelCapabilities {
  readonly text: boolean;
  readonly preview: boolean;
  readonly edit: boolean;
  readonly attachments: boolean;
  readonly threads: boolean;
  readonly reactions: boolean;
}

/** Text-only baseline; adapters explicitly enable any additional behavior. */
export const DEFAULT_CHANNEL_CAPABILITIES: Readonly<ChannelCapabilities> = Object.freeze({
  text: true,
  preview: false,
  edit: false,
  attachments: false,
  threads: false,
  reactions: false,
});

/** A preview is provisional and never acknowledges final delivery. */
export type DeliveryPhase = 'preview' | 'final';

export type DeliveryOutcome =
  | {
    readonly status: 'delivered';
    readonly phase: DeliveryPhase;
    readonly messageId: string;
  }
  | {
    /** Confirmed not sent, for example a local validation or API rejection. */
    readonly status: 'failed';
    readonly phase: DeliveryPhase;
    readonly reason: string;
  }
  | {
    /** The request may have been accepted; reconcile before another send. */
    readonly status: 'unknown';
    readonly phase: DeliveryPhase;
    readonly reason: string;
    readonly operationId?: string;
  };

/** Only a confirmed final delivery can satisfy the reply obligation. */
export function isFinalDelivery(outcome: DeliveryOutcome): boolean {
  return outcome.status === 'delivered' && outcome.phase === 'final'
    && typeof outcome.messageId === 'string' && outcome.messageId.trim().length > 0;
}

/**
 * Indicates eligibility for an explicit retry, never performs one. An unknown
 * transport result cannot be retried merely because no acknowledgement arrived.
 */
export function canRetryDelivery(outcome: DeliveryOutcome): boolean {
  return outcome.status === 'failed';
}
