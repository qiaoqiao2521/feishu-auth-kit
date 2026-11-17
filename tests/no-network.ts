import { afterAll, vi } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Keep all product tests offline, but permit the Telegram pilot's literal-loopback
// HTTP fixture. No DNS names or redirects can escape to a provider endpoint.
const nativeFetch = globalThis.fetch;
globalThis.fetch = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
  const url = new URL(input instanceof Request ? input.url : String(input));
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname)) {
    throw new Error('External network forbidden in product tests');
  }
  return nativeFetch(input, { ...init, redirect: 'error' });
});
vi.mock('node:http', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:http')>();
  return { ...actual,
    request: () => { throw new Error('Network forbidden'); },
    get: () => { throw new Error('Network forbidden'); } };
});
vi.mock('node:https', () => ({ request: () => { throw new Error('Network forbidden'); }, get: () => { throw new Error('Network forbidden'); } }));
const state = mkdtempSync(join(tmpdir(), 'feishu-tests-'));
process.env.FEISHU_AUTH_KIT_CONTINUATION_STORE = join(state, 'continuations.json');
afterAll(() => rmSync(state, { recursive: true, force: true }));
