/** Optional single-consumer local host. Node 24+; no server or agent dependency. */
import { DatabaseSync } from 'node:sqlite';
import { mkdirSync, chmodSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { AgentTurnRequest, EchoRunner, authorizeMessage, type AgentRunner,
  type AuthorizationGrant } from '../core/index.js';
import { TelegramApiError, type TelegramBotClient } from './client.js';
import { normalizeTelegramUpdate } from './normalizer.js';

export type TelegramDisposition = 'running' | 'sending' | 'delivered' | 'failed' | 'unknown'
  | 'execution_unknown' | 'run_failed' | 'denied' | 'ignored' | 'malformed';
export interface TelegramUpdateReceipt {
  updateId: string;
  disposition: TelegramDisposition;
  messageId?: string;
}

function updateId(value: unknown): string {
  if (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0) return String(value);
  if (typeof value === 'string' && /^(0|[1-9][0-9]*)$/.test(value)
    && BigInt(value) <= BigInt(Number.MAX_SAFE_INTEGER)) return value;
  throw new TypeError('Telegram update_id must be a nonnegative safe integer');
}

function processExists(pid: number): boolean {
  try { process.kill(pid, 0); return true; }
  catch (error) { return (error as NodeJS.ErrnoException).code !== 'ESRCH'; }
}

/**
 * Local SQLite ledger, bound to one bot. Only IDs/dispositions are persisted.
 * BEGIN IMMEDIATE serializes ownership claims; a dead local PID can be replaced.
 * A reused/live PID fails closed. Do not share this database across machines.
 */
export class TelegramPollStore {
  readonly accountId: string;
  #db: DatabaseSync;
  #owner = randomUUID();
  #closed = false;

  constructor(path: string, accountId: string) {
    if (!/^[1-9][0-9]*$/.test(accountId)) throw new TypeError('accountId must be the Telegram bot ID');
    this.accountId = accountId;
    const file = path === ':memory:' ? path : resolve(path);
    if (file !== ':memory:') mkdirSync(dirname(file), { recursive: true, mode: 0o700 });
    this.#db = new DatabaseSync(file);
    try {
      if (file !== ':memory:') chmodSync(file, 0o600);
      this.#db.exec(`PRAGMA busy_timeout=5000; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS consumer (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT, pid INTEGER);
        CREATE TABLE IF NOT EXISTS receipts (update_id TEXT PRIMARY KEY, disposition TEXT NOT NULL,
          message_id TEXT);`);
      this.transaction(() => {
        const schema = this.getMeta('schema');
        if (schema && schema !== 'channel-kit.telegram-poll.v1') throw new Error('Unsupported Telegram ledger schema');
        const account = this.getMeta('account_id');
        if (account && account !== accountId) throw new Error('Telegram ledger belongs to a different bot');
        const owner = this.#db.prepare('SELECT owner, pid FROM consumer WHERE id=1').get();
        if (owner?.owner && processExists(Number(owner.pid))) throw new Error('Telegram ledger already has a live consumer');
        this.setMeta('schema', 'channel-kit.telegram-poll.v1');
        this.setMeta('account_id', accountId);
        this.#db.prepare('INSERT OR REPLACE INTO consumer(id,owner,pid) VALUES(1,?,?)').run(this.#owner, process.pid);
        // Interrupted execution and delivery are different unknown operations.
        this.#db.exec(`UPDATE receipts SET disposition='execution_unknown' WHERE disposition='running';
          UPDATE receipts SET disposition='unknown' WHERE disposition='sending';`);
      });
    } catch (error) { this.#db.close(); throw error; }
  }

  private transaction<T>(body: () => T): T {
    this.#db.exec('BEGIN IMMEDIATE');
    try { const result = body(); this.#db.exec('COMMIT'); return result; }
    catch (error) { this.#db.exec('ROLLBACK'); throw error; }
  }
  private getMeta(key: string): string | undefined {
    const row = this.#db.prepare('SELECT value FROM metadata WHERE key=?').get(key);
    return row ? String(row.value) : undefined;
  }
  private setMeta(key: string, value: string): void {
    this.#db.prepare('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)').run(key, value);
  }
  getOffset(): string | undefined { return this.getMeta('offset'); }
  getReceipt(id: string): TelegramUpdateReceipt | null {
    const row = this.#db.prepare('SELECT disposition,message_id FROM receipts WHERE update_id=?').get(updateId(id));
    return row ? { updateId: id, disposition: row.disposition as TelegramDisposition,
      ...(row.message_id ? { messageId: String(row.message_id) } : {}) } : null;
  }
  claim(id: string): boolean {
    return Number(this.#db.prepare("INSERT OR IGNORE INTO receipts(update_id,disposition) VALUES(?,'running')")
      .run(updateId(id)).changes) === 1;
  }
  markSending(id: string): void {
    const changed = this.#db.prepare("UPDATE receipts SET disposition='sending' WHERE update_id=? AND disposition='running'")
      .run(updateId(id)).changes;
    if (Number(changed) !== 1) throw new Error('Telegram delivery claim is not executable');
  }
  finish(id: string, disposition: TelegramDisposition, messageId?: string): TelegramUpdateReceipt {
    if (disposition === 'running' || disposition === 'sending') throw new Error('Cannot acknowledge unfinished work');
    return this.transaction(() => {
      const changed = this.#db.prepare('UPDATE receipts SET disposition=?,message_id=? WHERE update_id=?')
        .run(disposition, messageId ?? null, updateId(id)).changes;
      if (Number(changed) !== 1) throw new Error('Telegram update has no durable claim');
      const next = (BigInt(id) + 1n).toString();
      const offset = this.getOffset();
      if (!offset || BigInt(next) > BigInt(offset)) this.setMeta('offset', next);
      return { updateId: id, disposition, ...(messageId ? { messageId } : {}) };
    });
  }
  close(): void {
    if (this.#closed) return;
    this.#db.prepare('DELETE FROM consumer WHERE id=1 AND owner=?').run(this.#owner);
    this.#db.close();
    this.#closed = true;
  }
}

type PollClient = Pick<TelegramBotClient, 'getMe' | 'getUpdates' | 'reply'>;

export class TelegramPollingHost {
  #busy = false;
  #verified = false;
  readonly runner: AgentRunner;
  constructor(private readonly options: {
    client: PollClient;
    store: TelegramPollStore;
    grants?: readonly AuthorizationGrant[];
    runner?: AgentRunner;
  }) { this.runner = options.runner ?? new EchoRunner(); }

  /** One bounded batch. No automatic retry of runner executions or sends. */
  async pollOnce(options: { timeout?: number; signal?: AbortSignal } = {}): Promise<TelegramUpdateReceipt[]> {
    if (this.#busy) throw new Error('Telegram poll is already in progress');
    this.#busy = true;
    try {
      if (!this.#verified) {
        const identity = await this.options.client.getMe();
        if (!identity.isBot || identity.id !== this.options.store.accountId) throw new Error('Telegram bot identity does not match accountId');
        this.#verified = true;
      }
      const updates = await this.options.client.getUpdates({ offset: this.options.store.getOffset(),
        timeout: options.timeout ?? 20, allowedUpdates: ['message'], signal: options.signal });
      const batch = updates.map((update) => ({ update, id: updateId(update.update_id) }))
        .sort((a, b) => BigInt(a.id) < BigInt(b.id) ? -1 : BigInt(a.id) > BigInt(b.id) ? 1 : 0);
      const results: TelegramUpdateReceipt[] = [];
      for (const { update, id } of batch) {
        if (options.signal?.aborted) break;
        const existing = this.options.store.getReceipt(id);
        if (existing) {
          const disposition = existing.disposition === 'sending' ? 'unknown'
            : existing.disposition === 'running' ? 'execution_unknown' : existing.disposition;
          results.push(this.options.store.finish(id, disposition, existing.messageId));
          continue;
        }
        const offset = this.options.store.getOffset();
        // Telegram retries may include old updates beyond local ledger retention.
        if (offset && BigInt(id) < BigInt(offset)) continue;
        if (!this.options.store.claim(id)) throw new Error('Telegram update claim failed');
        let envelope;
        try { envelope = normalizeTelegramUpdate(update, { accountId: this.options.store.accountId }); }
        catch { results.push(this.options.store.finish(id, 'malformed')); continue; }
        if (!envelope || envelope.getReplyContext().kind !== 'message' || !envelope.message.text.trim()) {
          results.push(this.options.store.finish(id, 'ignored')); continue;
        }
        const message = envelope.message;
        if (!authorizeMessage(message, this.options.grants).allowed) {
          results.push(this.options.store.finish(id, 'denied')); continue;
        }
        let result;
        try { result = await this.runner.run(AgentTurnRequest.fromChannelMessage(message)); }
        catch { results.push(this.options.store.finish(id, 'run_failed')); continue; }
        if (result.status !== 'completed' || !result.outputText.trim()) {
          results.push(this.options.store.finish(id, 'run_failed')); continue;
        }
        // Match Bot API text preflight before recording a network intent.
        if ([...result.outputText].length > 4096) {
          results.push(this.options.store.finish(id, 'failed')); continue;
        }
        if (options.signal?.aborted) {
          results.push(this.options.store.finish(id, 'run_failed')); break;
        }
        this.options.store.markSending(id); // durable intent precedes the network operation
        try {
          const receipt = await this.options.client.reply(message, result.outputText, { signal: options.signal });
          if (receipt.chatId !== message.conversationId) throw new Error('Unexpected reply destination');
          if (message.threadId !== undefined && receipt.threadId !== message.threadId) {
            throw new Error('Reply topic was not confirmed');
          }
          results.push(this.options.store.finish(id, 'delivered', receipt.messageId));
        } catch (error) {
          results.push(this.options.store.finish(id, error instanceof TelegramApiError ? 'failed' : 'unknown'));
        }
      }
      return results;
    } finally { this.#busy = false; }
  }
}
