import { afterEach, describe, expect, it, vi } from 'vitest';
import { DeviceFlowClient, DeviceFlowError } from '../src/device-flow.js';

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function pollClient(responses: Response[]) {
  vi.useFakeTimers();
  vi.setSystemTime(0);
  const fetchFn = vi.fn(async () => responses.shift()!);
  const sleeper = vi.fn(async (ms: number) => {
    vi.setSystemTime(Date.now() + ms);
  });
  return { client: new DeviceFlowClient('cli_a1b2', 'app-secret', { fetchFn, sleeper }), fetchFn, sleeper };
}

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status });
}

describe('DeviceFlowClient', () => {
  it('requestAuthorization uses form body and adds offline_access', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        device_code: 'dev-123',
        user_code: 'ABCD-EFGH',
        verification_uri: 'https://example.test/verify',
        verification_uri_complete: 'https://example.test/verify?code=ABCD-EFGH',
        expires_in: 600,
        interval: 5,
      }),
    });

    const client = new DeviceFlowClient('cli_a1b2', 'secret', { fetchFn: fetchMock });
    const auth = await client.requestAuthorization(['im:message:readonly']);

    expect(auth.device_code).toBe('dev-123');
    expect(fetchMock).toHaveBeenCalledTimes(1);

    const [url, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe('POST');
    expect(init.headers['Content-Type']).toBe('application/x-www-form-urlencoded');

    const bodyStr = init.body.toString();
    expect(bodyStr).toContain('client_id=cli_a1b2');
    expect(bodyStr).toContain('scope=im%3Amessage%3Areadonly+offline_access');
  });

  it.each([200, 400])('polls pending and slow_down with HTTP %i envelopes', async (status) => {
    const { client, fetchFn, sleeper } = pollClient([
      jsonResponse({ error: 'authorization_pending' }, status),
      jsonResponse({ error: 'slow_down' }, status),
      jsonResponse({ error: 'slow_down' }, status),
      jsonResponse({ access_token: 'token', refresh_token: 'refresh', expires_in: 7200 }),
    ]);

    await expect(client.pollForToken('device-code', { interval: 5, expiresIn: 100 }))
      .resolves.toMatchObject({ access_token: 'token', refresh_token: 'refresh', expires_in: 7200 });
    expect(sleeper.mock.calls.map(([ms]) => ms)).toEqual([5000, 5000, 10000, 15000]);
    expect(fetchFn).toHaveBeenCalledTimes(4);
  });

  it.each([401, 403, 429, 500, 503])('does not retry HTTP %i even with a pending body', async (status) => {
    const { client, fetchFn } = pollClient([jsonResponse({ error: 'authorization_pending' }, status)]);
    await expect(client.pollForToken('device-code')).rejects.toThrow(`HTTP error ${status}`);
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it('does not accept an access token in an HTTP 400 response', async () => {
    const { client } = pollClient([jsonResponse({ access_token: 'token' }, 400)]);
    await expect(client.pollForToken('device-code')).rejects.toThrow('HTTP error 400');
  });

  it('reports terminal OAuth details without known credentials', async () => {
    const { client, fetchFn } = pollClient([jsonResponse({
      error: 'access_denied',
      error_description: 'Denied device-code for app-secret',
    }, 400)]);
    await expect(client.pollForToken('device-code'))
      .rejects.toThrow('Denied [REDACTED] for [REDACTED]');
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it.each([null, [], 'invalid', {}])('rejects a malformed successful token envelope: %j', async (payload) => {
    const { client } = pollClient([jsonResponse(payload)]);
    await expect(client.pollForToken('device-code')).rejects.toBeInstanceOf(DeviceFlowError);
  });

  it('reports non-JSON HTTP errors without returning the response body', async () => {
    const { client } = pollClient([new Response('proxy app-secret device-code', { status: 400 })]);
    await expect(client.pollForToken('device-code')).rejects.toThrow(/^HTTP error 400$/);
  });

  it('does not poll when the initial wait reaches expiration', async () => {
    const { client, fetchFn, sleeper } = pollClient([]);
    await expect(client.pollForToken('device-code', { interval: 5, expiresIn: 3 }))
      .rejects.toThrow('Device code expired');
    expect(sleeper).toHaveBeenCalledWith(3000);
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it('never passes a negative wait when clock reads cross expiration', async () => {
    vi.spyOn(Date, 'now')
      .mockReturnValueOnce(0)
      .mockReturnValueOnce(900)
      .mockReturnValue(1100);
    const sleeper = vi.fn(async (ms: number) => {
      expect(ms).toBeGreaterThanOrEqual(0);
    });
    const fetchFn = vi.fn();
    const client = new DeviceFlowClient('cli_a1b2', 'app-secret', { fetchFn, sleeper });
    await expect(client.pollForToken('device-code', { expiresIn: 1 }))
      .rejects.toThrow('Device code expired');
    expect(sleeper).toHaveBeenCalledWith(100);
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it('stops after pending when the remaining wait reaches expiration', async () => {
    const { client, fetchFn, sleeper } = pollClient([jsonResponse({ error: 'authorization_pending' }, 400)]);
    await expect(client.pollForToken('device-code', { interval: 5, expiresIn: 7 }))
      .rejects.toThrow('Device code expired');
    expect(sleeper.mock.calls.map(([ms]) => ms)).toEqual([5000, 2000]);
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it('does not echo the authorization endpoint HTTP response body', async () => {
    const fetchFn = vi.fn(async () => new Response('proxy app-secret', { status: 502 }));
    const client = new DeviceFlowClient('cli_a1b2', 'app-secret', { fetchFn });
    await expect(client.requestAuthorization([])).rejects.toThrow(/^HTTP error 502$/);
  });
});
