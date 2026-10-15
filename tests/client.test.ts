import { describe, expect, it, vi } from 'vitest';
import {
  FeishuApiError,
  FeishuAuthClient,
  FeishuAuthKitError,
  buildPermissionUrl,
} from '../src/client.js';

describe('FeishuAuthClient', () => {
  it('buildPermissionUrl includes scope, token type and source', () => {
    const url = buildPermissionUrl('cli_a1b2', {
      scopes: ['offline_access', 'im:message:readonly'],
      brand: 'feishu',
      tokenType: 'user',
      opFrom: 'feishu-auth-kit',
    });

    expect(url.startsWith('https://open.feishu.cn/app/cli_a1b2/auth?')).toBe(true);
    expect(url).toContain('q=offline_access%2Cim%3Amessage%3Areadonly');
    expect(url).toContain('token_type=user');
    expect(url).toContain('op_from=feishu-auth-kit');
  });

  it('getAppInfo parses scopes and token types', async () => {
    const originalFetch = globalThis.fetch;
    const fetchMock = vi.fn();

    // 1st call: tenant_access_token
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        code: 0,
        tenant_access_token: 'tenant-token',
        expire: 7200,
      }),
    });

    // 2nd call: app info
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        code: 0,
        data: {
          app: {
            app_id: 'cli_a1b2',
            creator_id: 'ou_creator',
            owner: { owner_id: 'ou_owner', owner_type: 2 },
            scopes: [
              {
                scope: 'im:message:readonly',
                token_types: ['user'],
              },
              {
                scope: 'application:application:self_manage',
                token_types: ['tenant'],
              },
            ],
          },
        },
      }),
    });

    globalThis.fetch = fetchMock;

    const client = new FeishuAuthClient('cli_a1b2', 'secret');
    const appInfo = await client.getAppInfo();

    expect(appInfo.app_id).toBe('cli_a1b2');
    expect(appInfo.creator_id).toBe('ou_creator');
    expect(appInfo.owner_open_id).toBe('ou_owner');
    expect(appInfo.effective_owner_open_id).toBe('ou_owner');
    expect(appInfo.scopes[0].token_types).toEqual(['user']);

    const userScopes = await client.getGrantedScopes({ tokenType: 'user' });
    const tenantScopes = await client.getGrantedScopes({ tokenType: 'tenant' });

    expect(userScopes).toEqual(['im:message:readonly']);
    expect(tenantScopes).toEqual(['application:application:self_manage']);

    globalThis.fetch = originalFetch;
  });

  it.each([21001, '21001'])('diagnoses access policy denial with HTTP 200 and code %s', async (code) => {
    const message = 'Access denied by access control policy. Contact the administrator for assistance.';
    const fetchFn = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      code,
      msg: message,
      log_id: 'body-log',
      access_token: 'must-not-appear',
    }), { status: 200, headers: { 'x-tt-logid': 'header-log' } }));
    const client = new FeishuAuthClient('cli_test', 'app-secret', { fetchFn });

    const error = await client.getTenantAccessToken().catch((failure: unknown) => failure);

    expect(error).toBeInstanceOf(FeishuApiError);
    expect(error).toBeInstanceOf(FeishuAuthKitError);
    expect(error).toMatchObject({
      code, httpStatus: 200, logId: 'header-log', apiMessage: message,
      hint: expect.stringContaining('IP 白名单'),
      message: expect.stringContaining(message),
    });
    expect((error as FeishuApiError).hint).toContain('企业管理员');
    expect(JSON.stringify(error)).not.toContain('must-not-appear');
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it('retains business code, HTTP status and log ID for a non-2xx response', async () => {
    const fetchFn = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      code: 99991672,
      msg: 'Forbidden',
      error: { log_id: 'body-log' },
    }), { status: 403, headers: { 'X-Tt-Logid': 'http-log' } }));
    const client = new FeishuAuthClient('cli_test', 'app-secret', { fetchFn });

    await expect(client.getTenantAccessToken()).rejects.toMatchObject({
      name: 'FeishuApiError', message: 'Forbidden',
      code: 99991672, httpStatus: 403, logId: 'http-log', hint: null,
    });
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it.each([
    { log_id: 'root-log' },
    { error: { log_id: 'nested-log' } },
  ])('uses the payload log ID when the response header is absent: %j', async (details) => {
    const fetchFn = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      code: 10003, msg: 'invalid param', ...details,
    }), { status: 200 }));
    const client = new FeishuAuthClient('cli_test', 'app-secret', { fetchFn });

    await expect(client.getTenantAccessToken()).rejects.toMatchObject({
      message: 'invalid param', code: 10003, httpStatus: 200,
      logId: 'log_id' in details ? details.log_id : details.error.log_id,
      hint: null,
    });
  });

  it('reports non-JSON HTTP failures without copying the response body', async () => {
    const fetchFn = vi.fn().mockResolvedValue(new Response('app-secret access-token', {
      status: 502, headers: { 'x-tt-logid': 'gateway-log' },
    }));
    const client = new FeishuAuthClient('cli_test', 'app-secret', { fetchFn });

    await expect(client.getTenantAccessToken()).rejects.toMatchObject({
      message: 'HTTP error 502', code: null, httpStatus: 502, logId: 'gateway-log',
    });
  });

  it('redacts credentials echoed in API messages', async () => {
    const fetchFn = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        code: 0, tenant_access_token: 'tenant-private-token', expire: 7200,
      })))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        code: 10003, msg: 'invalid app-private-secret / tenant-private-token',
        app_secret: 'app-private-secret', access_token: 'tenant-private-token',
      }), { status: 400 }));
    const client = new FeishuAuthClient('cli_test', 'app-private-secret', { fetchFn });

    const error = await client.getAppInfo().catch((failure: unknown) => failure);

    expect(error).toMatchObject({ message: 'invalid [REDACTED] / [REDACTED]' });
    expect(JSON.stringify(error)).not.toContain('app-private-secret');
    expect(JSON.stringify(error)).not.toContain('tenant-private-token');
    expect(fetchFn).toHaveBeenCalledTimes(2);
  });

  it('preserves ordinary error construction and inheritance', () => {
    const error = new FeishuApiError('existing message');

    expect(error).toBeInstanceOf(Error);
    expect(error).toBeInstanceOf(FeishuAuthKitError);
    expect(error.message).toBe('existing message');
    expect(error.name).toBe('FeishuApiError');
    expect(error.code).toBeNull();
    expect(error.httpStatus).toBeNull();
    expect(error.logId).toBeNull();
    expect(error.hint).toBeNull();
  });
});
