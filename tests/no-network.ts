import { vi } from 'vitest';
// Tests may inject synthetic FetchLike implementations, but never use real fetch.
globalThis.fetch = vi.fn(async () => { throw new Error('Network forbidden in product tests'); });
vi.mock('node:http', () => ({ request: () => { throw new Error('Network forbidden'); }, get: () => { throw new Error('Network forbidden'); } }));
vi.mock('node:https', () => ({ request: () => { throw new Error('Network forbidden'); }, get: () => { throw new Error('Network forbidden'); } }));
