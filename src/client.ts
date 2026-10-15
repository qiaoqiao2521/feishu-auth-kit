import { openPlatformDomain, resolveDomains, type DomainSet } from "./domains.js";
import type { AppInfo, ScopeGrant, TenantAccessToken } from "./models.js";

export class FeishuAuthKitError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "FeishuAuthKitError";
  }
}

export class FeishuApiError extends FeishuAuthKitError {
  readonly code: number | string | null;
  readonly httpStatus: number | null;
  readonly logId: string | null;
  readonly apiMessage: string;
  readonly hint: string | null;

  constructor(
    message: string,
    details: {
      code?: number | string | null;
      httpStatus?: number | null;
      logId?: string | null;
    } = {},
  ) {
    const hint = String(details.code) === "21001"
      ? "飞书访问控制策略拒绝了请求，请检查应用安全设置中的出口 IP 白名单，并联系企业管理员检查 API 访问策略。"
      : null;
    super(hint ? `${message} ${hint}` : message);
    this.name = "FeishuApiError";
    this.code = details.code ?? null;
    this.httpStatus = details.httpStatus ?? null;
    this.logId = details.logId ?? null;
    this.apiMessage = message;
    this.hint = hint;
  }
}

export function buildPermissionUrl(
  appId: string,
  scopesOrOptions?: Iterable<string> | {
    scopes?: Iterable<string>;
    brand?: string;
    tokenType?: string;
    opFrom?: string;
  },
  options?: {
    brand?: string;
    tokenType?: string;
    opFrom?: string;
  }
): string {
  let scopes: Iterable<string> = [];
  let opts = options;
  if (scopesOrOptions) {
    if (Array.isArray(scopesOrOptions) || (typeof (scopesOrOptions as any)[Symbol.iterator] === "function" && typeof scopesOrOptions !== "string")) {
      scopes = scopesOrOptions as Iterable<string>;
    } else if (typeof scopesOrOptions === "object") {
      const obj = scopesOrOptions as any;
      scopes = obj.scopes || [];
      opts = { ...obj, ...options };
    }
  }
  const brand = opts?.brand ?? "feishu";
  const tokenType = opts?.tokenType ?? "tenant";
  const opFrom = opts?.opFrom ?? "feishu-auth-kit";

  const scopeList = Array.from(scopes)
    .map((s) => s.trim())
    .filter(Boolean);

  const query = new URLSearchParams({
    q: scopeList.join(","),
    op_from: opFrom,
    token_type: tokenType,
  });

  return `${openPlatformDomain(brand)}/app/${appId}/auth?${query.toString()}`;
}

export interface FetchLike {
  (input: string | URL, init?: RequestInit): Promise<Response>;
}

export class FeishuAuthClient {
  readonly appId: string;
  readonly appSecret: string;
  readonly brand: string;
  readonly timeoutMs: number;
  readonly domains: DomainSet;
  readonly fetchFn: FetchLike;

  private tenantToken: TenantAccessToken | null = null;
  private appInfo: AppInfo | null = null;

  constructor(
    appId: string,
    appSecret: string,
    options?: {
      brand?: string;
      timeoutMs?: number;
      fetchFn?: FetchLike;
    }
  ) {
    this.appId = appId;
    this.appSecret = appSecret;
    this.brand = options?.brand ?? "feishu";
    this.timeoutMs = options?.timeoutMs ?? 30000;
    this.domains = resolveDomains(this.brand);
    this.fetchFn = options?.fetchFn ?? globalThis.fetch;
  }

  // Backwards compatible getter property
  get app_id(): string {
    return this.appId;
  }

  get app_secret(): string {
    return this.appSecret;
  }

  private async requestJson(
    url: string,
    options: {
      method?: string;
      headers?: Record<string, string>;
      body?: any;
      params?: Record<string, string>;
    } = {}
  ): Promise<any> {
    let finalUrl = url;
    if (options.params) {
      const sp = new URLSearchParams(options.params);
      finalUrl += (url.includes("?") ? "&" : "?") + sp.toString();
    }

    const init: RequestInit = {
      method: options.method ?? "GET",
      headers: {
        Accept: "application/json",
        ...options.headers,
      },
      signal: AbortSignal.timeout(this.timeoutMs),
    };

    if (options.body !== undefined) {
      if (typeof options.body === "string" || options.body instanceof URLSearchParams) {
        init.body = options.body;
      } else {
        init.body = JSON.stringify(options.body);
        (init.headers as Record<string, string>)["Content-Type"] = "application/json";
      }
    }

    const response = await this.fetchFn(finalUrl, init);
    let payload: any;
    try {
      payload = await response.json();
    } catch (error) {
      if (response.ok) throw error;
    }

    const code = payload && typeof payload === "object" ? payload.code : null;
    const businessError = code !== 0 && code !== undefined && code !== null;
    if (!response.ok || businessError) {
      const rawMessage = payload?.msg || payload?.message;
      let message = typeof rawMessage === "string"
        ? rawMessage
        : response.ok ? "Unknown Feishu API error" : `HTTP error ${response.status}`;
      // An API or proxy may echo credentials in its error message.
      const credentials = [this.appSecret];
      for (const [key, value] of Object.entries(options.headers || {})) {
        if (key.toLowerCase() === "authorization") {
          credentials.push(value.replace(/^Bearer\s+/i, ""));
        }
      }
      for (const credential of credentials) {
        if (credential) message = message.split(credential).join("[REDACTED]");
      }
      const logIdCandidates = [
        response.headers?.get?.("x-tt-logid"),
        payload?.log_id,
        payload?.error?.log_id,
      ];
      const logId = logIdCandidates.find((value) => typeof value === "string" && value.trim());
      throw new FeishuApiError(message, {
        code: typeof code === "number" || typeof code === "string" ? code : null,
        httpStatus: Number.isInteger(response.status) ? response.status : null,
        logId: logId ? logId.trim() : null,
      });
    }
    return payload;
  }

  async requestWithTenantToken(
    urlOrPath: string,
    options: {
      method?: string;
      headers?: Record<string, string>;
      body?: any;
      params?: Record<string, string>;
    } = {}
  ): Promise<any> {
    const token = (await this.getTenantAccessToken()).token;
    const url = urlOrPath.startsWith("http://") || urlOrPath.startsWith("https://")
      ? urlOrPath
      : `${this.domains.openBase}${urlOrPath.startsWith("/") ? "" : "/"}${urlOrPath}`;
    return this.requestJson(url, {
      ...options,
      headers: {
        Authorization: `Bearer ${token}`,
        ...options.headers,
      },
    });
  }

  async getTenantAccessToken(options?: { forceRefresh?: boolean }): Promise<TenantAccessToken> {
    if (this.tenantToken && !options?.forceRefresh) {
      return this.tenantToken;
    }

    const payload = await this.requestJson(this.domains.tenantTokenUrl, {
      method: "POST",
      body: {
        app_id: this.appId,
        app_secret: this.appSecret,
      },
    });

    const token: TenantAccessToken = {
      token: String(payload.tenant_access_token),
      expire: payload.expire ?? null,
    };
    this.tenantToken = token;
    return token;
  }

  static parseAppInfo(payload: Record<string, any>): AppInfo {
    const app = payload?.data?.app || payload?.app || payload?.data || {};
    const owner = app.owner || {};
    const ownerType = owner.owner_type ?? owner.type ?? null;
    const ownerOpenId = owner.owner_id ?? null;
    const creatorId = app.creator_id ?? null;

    let effectiveOwnerOpenId: string | null = null;
    if (ownerType === 2 && ownerOpenId) {
      effectiveOwnerOpenId = ownerOpenId;
    } else {
      effectiveOwnerOpenId = creatorId || ownerOpenId || null;
    }

    const rawScopes = app.scopes || app.online_version?.scopes || [];
    const scopes: ScopeGrant[] = [];
    for (const item of rawScopes) {
      if (item && item.scope) {
        scopes.push({
          scope: String(item.scope),
          token_types: Array.isArray(item.token_types) ? item.token_types.map(String) : [],
        });
      }
    }

    return {
      app_id: String(app.app_id || app.id || app.cli_app_id || ""),
      name: app.name ?? null,
      creator_id: creatorId,
      owner_open_id: ownerOpenId,
      owner_type: ownerType,
      effective_owner_open_id: effectiveOwnerOpenId,
      scopes,
      raw_app: app,
    };
  }

  async getAppInfo(appId: string = "me", options?: { forceRefresh?: boolean }): Promise<AppInfo> {
    if (this.appInfo && !options?.forceRefresh && (appId === "me" || appId === this.appInfo.app_id)) {
      return this.appInfo;
    }

    const tenantToken = (await this.getTenantAccessToken(options)).token;
    const payload = await this.requestJson(`${this.domains.appInfoBase}/${appId}`, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${tenantToken}`,
      },
      params: {
        lang: "en_us",
      },
    });

    const info = FeishuAuthClient.parseAppInfo(payload);
    this.appInfo = info;
    return info;
  }

  async getGrantedScopes(options?: {
    tokenType?: string | null;
    appInfo?: AppInfo | null;
  }): Promise<string[]> {
    const current = options?.appInfo || (await this.getAppInfo());
    const scopes: string[] = [];
    for (const item of current.scopes) {
      if (options?.tokenType && item.token_types && item.token_types.length > 0 && !item.token_types.includes(options.tokenType)) {
        continue;
      }
      scopes.push(item.scope);
    }
    return scopes;
  }

  buildPermissionUrl(
    appId: string,
    options: {
      scopes: Iterable<string>;
      tokenType?: string;
      opFrom?: string;
    }
  ): string {
    return buildPermissionUrl(appId, options.scopes, {
      brand: this.brand,
      tokenType: options.tokenType,
      opFrom: options.opFrom,
    });
  }
}
