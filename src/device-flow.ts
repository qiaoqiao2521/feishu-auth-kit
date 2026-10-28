import { resolveDomains, type DomainSet } from "./domains.js";
import type { DeviceAuthorization, DeviceToken } from "./models.js";
import type { FetchLike } from "./client.js";

export class DeviceFlowError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DeviceFlowError";
  }
}

export class DeviceFlowClient {
  readonly appId: string;
  readonly appSecret: string;
  readonly brand: string;
  readonly domains: DomainSet;
  readonly timeoutMs: number;
  readonly fetchFn: FetchLike;
  readonly sleeper: (ms: number) => Promise<void>;

  constructor(
    appId: string,
    appSecret: string,
    options?: {
      brand?: string;
      timeoutMs?: number;
      fetchFn?: FetchLike;
      sleeper?: (ms: number) => Promise<void>;
    }
  ) {
    this.appId = appId;
    this.appSecret = appSecret;
    this.brand = options?.brand ?? "feishu";
    this.domains = resolveDomains(this.brand);
    this.timeoutMs = options?.timeoutMs ?? 30000;
    this.fetchFn = options?.fetchFn ?? globalThis.fetch;
    this.sleeper = options?.sleeper ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
  }

  static scopeString(scopes: Iterable<string>): string {
    const items: string[] = [];
    const seen = new Set<string>();
    for (const scope of scopes) {
      const normalized = scope.trim();
      if (normalized && !seen.has(normalized)) {
        items.push(normalized);
        seen.add(normalized);
      }
    }
    if (!seen.has("offline_access")) {
      items.push("offline_access");
    }
    return items.join(" ");
  }

  private async responsePayload(response: Response): Promise<Record<string, any>> {
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new DeviceFlowError(response.ok
        ? "Invalid device flow response"
        : `HTTP error ${response.status}`);
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new DeviceFlowError(response.ok
        ? "Invalid device flow response"
        : `HTTP error ${response.status}`);
    }
    return payload as Record<string, any>;
  }

  private errorDetail(payload: Record<string, any>, fallback: string, deviceCode?: string): string {
    const detail = typeof payload.error_description === "string" && payload.error_description
      ? payload.error_description
      : typeof payload.error === "string" && payload.error ? payload.error : fallback;
    let message = detail;
    const basic = Buffer.from(`${this.appId}:${this.appSecret}`).toString("base64");
    for (const credential of [this.appSecret, basic, deviceCode]) {
      if (credential) message = message.split(credential).join("[REDACTED]");
    }
    return message;
  }

  async requestAuthorization(scopes: Iterable<string>): Promise<DeviceAuthorization> {
    const scopeStr = DeviceFlowClient.scopeString(scopes);
    const basic = Buffer.from(`${this.appId}:${this.appSecret}`).toString("base64");

    const body = new URLSearchParams({
      client_id: this.appId,
      scope: scopeStr,
    });

    const response = await this.fetchFn(this.domains.deviceAuthorizationUrl, {
      method: "POST",
      headers: {
        Authorization: `Basic ${basic}`,
        "Content-Type": "application/x-www-form-urlencoded",
        Accept: "application/json",
      },
      body: body.toString(),
      signal: AbortSignal.timeout(this.timeoutMs),
    });

    const payload = await this.responsePayload(response);
    if (!response.ok || payload.error) {
      throw new DeviceFlowError(this.errorDetail(payload, `HTTP error ${response.status}`));
    }

    return {
      device_code: String(payload.device_code),
      user_code: String(payload.user_code),
      verification_uri: String(payload.verification_uri),
      verification_uri_complete: String(payload.verification_uri_complete || payload.verification_uri),
      expires_in: Number(payload.expires_in ?? 240),
      interval: Number(payload.interval ?? 5),
    };
  }

  async pollForToken(
    deviceCode: string,
    options?: {
      interval?: number;
      expiresIn?: number;
    }
  ): Promise<DeviceToken> {
    const intervalSec = options?.interval ?? 5;
    const expiresInSec = options?.expiresIn ?? 240;
    const deadline = Date.now() + expiresInSec * 1000;
    let currentInterval = Math.max(intervalSec, 1);

    while (true) {
      const remainingMs = deadline - Date.now();
      if (remainingMs <= 0) break;
      await this.sleeper(Math.min(currentInterval * 1000, remainingMs));
      if (Date.now() >= deadline) break;

      const body = new URLSearchParams({
        grant_type: "urn:ietf:params:oauth:grant-type:device_code",
        device_code: deviceCode,
        client_id: this.appId,
        client_secret: this.appSecret,
      });

      const response = await this.fetchFn(this.domains.oauthTokenUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          Accept: "application/json",
        },
        body: body.toString(),
        signal: AbortSignal.timeout(this.timeoutMs),
      });

      // OAuth device polling uses HTTP 400 for pending/slow_down. Other HTTP
      // failures must remain terminal even if their body contains those names.
      if (!response.ok && response.status !== 400) {
        throw new DeviceFlowError(`HTTP error ${response.status}`);
      }
      const payload = await this.responsePayload(response);
      const error = payload.error;

      if (response.ok && !error && typeof payload.access_token === "string" && payload.access_token) {
        return {
          access_token: String(payload.access_token),
          refresh_token: payload.refresh_token ?? null,
          expires_in: payload.expires_in ?? null,
          refresh_expires_in: payload.refresh_expires_in ?? null,
          scope: payload.scope ?? null,
        };
      }

      if (error === "authorization_pending") {
        continue;
      }
      if (error === "slow_down") {
        currentInterval += 5;
        continue;
      }

      throw new DeviceFlowError(this.errorDetail(
        payload,
        response.ok ? "Device flow failed" : `HTTP error ${response.status}`,
        deviceCode,
      ));
    }

    throw new DeviceFlowError("Device code expired before authorization completed");
  }
}
