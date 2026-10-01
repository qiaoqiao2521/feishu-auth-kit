import { afterEach, describe, expect, it, vi } from 'vitest';
import { FeishuAuthClient } from '../src/client.js';

const response = (expire: unknown = 100) => ({ok:true,json:async()=>({tenant_access_token:'SYNTHETIC_TOKEN',expire})}) as Response;
afterEach(() => vi.restoreAllMocks());
describe('tenant cache lifetime', () => {
  it('expires and preserves explicit force refresh', async () => {
    let now = 1000;vi.spyOn(performance,'now').mockImplementation(()=>now);
    const fetchFn=vi.fn(async()=>response());const client=new FeishuAuthClient('cli_SYNTHETIC','SYNTHETIC',{fetchFn});
    const first=await client.getTenantAccessToken();now=81000;
    expect(await client.getTenantAccessToken()).toBe(first);
    now=92000;expect(await client.getTenantAccessToken()).not.toBe(first);
    await client.getTenantAccessToken({forceRefresh:true});expect(fetchFn).toHaveBeenCalledTimes(3);
  });
  it.each([0,-1,'invalid',Infinity,NaN])('does not cache invalid lifetime %s', async ttl => {
    const fetchFn=vi.fn(async()=>response(ttl));const client=new FeishuAuthClient('cli_SYNTHETIC','SYNTHETIC',{fetchFn});
    await client.getTenantAccessToken();await client.getTenantAccessToken();expect(fetchFn).toHaveBeenCalledTimes(2);
  });
  it('coalesces concurrent refreshes and recovers after failure', async () => {
    const fetchFn=vi.fn().mockRejectedValueOnce(new Error('SYNTHETIC_FAILURE')).mockImplementation(async()=>response(7200));
    const client=new FeishuAuthClient('cli_SYNTHETIC','SYNTHETIC',{fetchFn});
    await expect(client.getTenantAccessToken()).rejects.toThrow('SYNTHETIC_FAILURE');
    const result=await Promise.all(Array.from({length:4},()=>client.getTenantAccessToken()));
    expect(fetchFn).toHaveBeenCalledTimes(2);expect(result.every(token=>token===result[0])).toBe(true);
  });
});
