import fs from 'node:fs';
import { describe, expect, it } from 'vitest';
import { normalizeTelegramUpdate, TelegramNormalizationError } from '../src/telegram/normalizer.js';

const cases = JSON.parse(fs.readFileSync(new URL('../fixtures/telegram/messages.json', import.meta.url), 'utf8'));

describe('Telegram normalization', () => {
  it.each(cases)('normalizes $name', ({ update, expected }) => {
    const envelope = normalizeTelegramUpdate(update, { accountId: '123' })!;
    expect(envelope.message).toMatchObject({ platform: 'telegram', accountId: '123', ...expected });
    expect(envelope.getReplyContext().updateId).toBe(String(update.update_id));
    expect(JSON.stringify(envelope)).not.toContain('FAKE_SECRET');
    expect(JSON.stringify(envelope)).not.toContain('private quoted text');
    expect(JSON.stringify(envelope)).not.toContain('file_path');
  });

  it.each(['document', 'audio', 'voice', 'video', 'animation', 'video_note', 'sticker'])
    ('keeps %s as an opaque reference without raw transport metadata', (field) => {
      const envelope = normalizeTelegramUpdate({ update_id: 1, message: {
        message_id: 2, chat: { id: 3, type: 'private' }, from: { id: 4 },
        [field]: { file_id: 'opaque-file-id', file_unique_id: 'private-unique', file_name: 'input.dat',
          mime_type: 'application/octet-stream', file_size: 1024, file_path: 'credential-url' },
      } }, { accountId: '123' })!;
      expect(envelope.message.attachments[0]).toMatchObject({ id: 'opaque-file-id', name: 'input.dat', sizeBytes: 1024 });
      expect(JSON.stringify(envelope)).not.toContain('private-unique');
      expect(JSON.stringify(envelope)).not.toContain('credential-url');
    });

  it('keeps callback fields private and uses the clicking user as sender', () => {
    const envelope = normalizeTelegramUpdate({ update_id: 10, callback_query: {
      id: 'opaque-callback', data: 'sensitive-route-value', from: { id: 55 },
      message: { message_id: 20, date: 0, chat: { id: -10033, type: 'supergroup' },
        from: { id: 123 }, text: 'old bot text' },
    } }, { accountId: '123' })!;
    expect(envelope.message.senderId).toBe('55');
    expect(envelope.message.text).toBe('');
    expect(envelope.getReplyContext()).toMatchObject({ kind: 'callback_query',
      callbackQueryId: 'opaque-callback', callbackData: 'sensitive-route-value' });
    const serialized = JSON.stringify(envelope);
    expect(serialized).not.toContain('sensitive-route-value');
    expect(serialized).not.toContain('opaque-callback');
    expect(serialized).not.toContain('old bot text');
  });

  it('preserves edited event kind for host admission decisions', () => {
    const envelope = normalizeTelegramUpdate({ update_id: 1, edited_message: {
      message_id: 2, chat: { id: 3, type: 'private' }, from: { id: 4 }, text: 'edited',
    } }, { accountId: '123' })!;
    expect(envelope.getReplyContext().kind).toBe('edited_message');
  });

  it('preserves oversized string IDs and rejects numbers already rounded by a caller', () => {
    const update = { update_id: '9007199254740993', message: {
      message_id: '9007199254740995', chat: { id: '-9007199254740997', type: 'supergroup' },
      from: { id: '9007199254740999' }, text: 'hello',
    } };
    expect(normalizeTelegramUpdate(update, { accountId: '123' })!.message.senderId).toBe('9007199254740999');
    expect(() => normalizeTelegramUpdate({ ...update, message: { ...update.message,
      message_id: Number.MAX_SAFE_INTEGER + 1 } }, { accountId: '123' })).toThrow(TelegramNormalizationError);
  });

  it('does not normalize service, business, or unscoped inline callback updates as prompts', () => {
    expect(normalizeTelegramUpdate({ update_id: 1, message: { message_id: 2,
      chat: { id: 3, type: 'private' }, from: { id: 4 }, new_chat_title: 'changed' } }, { accountId: '123' })).toBeNull();
    expect(normalizeTelegramUpdate({ update_id: 1, business_message: { text: 'not supported' } }, { accountId: '123' })).toBeNull();
    expect(normalizeTelegramUpdate({ update_id: 1, callback_query: { id: 'callback',
      inline_message_id: 'inline', from: { id: 4 }, data: 'route' } }, { accountId: '123' })).toBeNull();
  });

  it('does not substitute unknown group sender identity with chat identity', () => {
    expect(() => normalizeTelegramUpdate({ update_id: 1, message: {
      message_id: 2, chat: { id: -3, type: 'group' }, text: 'who sent this?',
    } }, { accountId: '123' })).toThrow(TelegramNormalizationError);
  });
});
