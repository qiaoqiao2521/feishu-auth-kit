import { FeishuApiError, FeishuAuthClient } from "./client.js";

export const AI_AGENT_PING_PATH = "/open-apis/bot/v1/openclaw_bot/ping";

export interface FeishuApiErrorDetails {
  code?: number | string;
  http_status?: number;
  log_id?: string;
  hint?: string;
}

export function apiErrorDetails(error: unknown): FeishuApiErrorDetails | undefined {
  if (!(error instanceof FeishuApiError)) return undefined;
  const details: FeishuApiErrorDetails = {};
  if (error.code !== null) details.code = error.code;
  if (error.httpStatus !== null) details.http_status = error.httpStatus;
  if (error.logId !== null) details.log_id = error.logId;
  if (error.hint !== null) details.hint = error.hint;
  return Object.keys(details).length ? details : undefined;
}

export interface FeishuProbeResult {
  ok: boolean;
  app_id?: string | null;
  bot_name?: string | null;
  bot_open_id?: string | null;
  error?: string | null;
  api_error?: FeishuApiErrorDetails;
  raw?: Record<string, any>;
}

export async function registerAiAgent(client: FeishuAuthClient): Promise<FeishuProbeResult> {
  try {
    const payload = await client.requestWithTenantToken(AI_AGENT_PING_PATH, {
      method: "POST",
      body: { needBotInfo: true },
    });

    const botInfo = payload?.data?.pingBotInfo || {};
    return {
      ok: true,
      app_id: client.appId,
      bot_name: botInfo.botName ?? null,
      bot_open_id: botInfo.botID ?? null,
      raw: payload,
    };
  } catch (exc: any) {
    const details = apiErrorDetails(exc);
    return {
      ok: false,
      app_id: client.appId,
      error: exc?.message || String(exc),
      ...(details ? { api_error: details } : {}),
    };
  }
}

export async function probeAiAgentCredentials(
  appId: string,
  appSecret: string,
  options?: {
    brand?: string;
    fetchFn?: any;
  }
): Promise<FeishuProbeResult> {
  const client = new FeishuAuthClient(appId, appSecret, options);
  return registerAiAgent(client);
}
