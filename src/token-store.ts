import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { randomUUID } from "node:crypto";
import type { DeviceToken } from "./models.js";

export function defaultTokenStorePath(): string {
  const configured = process.env.FEISHU_AUTH_KIT_TOKEN_STORE;
  if (configured) {
    return path.resolve(configured.replace(/^~(?=$|\/|\\)/, os.homedir()));
  }
  const dataHome = process.env.XDG_DATA_HOME;
  const base = dataHome
    ? path.resolve(dataHome.replace(/^~(?=$|\/|\\)/, os.homedir()))
    : path.join(os.homedir(), ".local", "share");
  return path.join(base, "feishu-auth-kit", "user_tokens.json");
}

export interface StoredUserToken {
  app_id: string;
  user_open_id: string;
  access_token: string;
  refresh_token?: string | null;
  expires_at?: number | null;
  refresh_expires_at?: number | null;
  scope?: string | null;
}

export interface TokenStatus {
  app_id: string;
  user_open_id: string;
  exists: boolean;
  storage_path: string;
  scope?: string | null;
  expires_at?: number | null;
  refresh_expires_at?: number | null;
}

export class FileTokenStore {
  readonly path: string;

  constructor(customPath?: string) {
    if (customPath) {
      this.path = path.resolve(customPath.replace(/^~(?=$|\/|\\)/, os.homedir()));
    } else {
      this.path = defaultTokenStorePath();
    }
  }

  static storageKey(app_id: string, user_open_id: string): string {
    return `${app_id}:${user_open_id}`;
  }

  private prepareParent(): void {
    const parent = path.dirname(this.path);
    fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
    const info = fs.lstatSync(parent);
    if (info.isSymbolicLink() || (info.mode & 0o022)) throw new Error("Token storage directory is unsafe");
  }

  private transaction<T>(operation: () => T): T {
    this.prepareParent();
    const lock = `${this.path}.lock`;
    const deadline = performance.now() + 2000;
    while (true) {
      try { fs.mkdirSync(lock, { mode: 0o700 }); break; }
      catch (error: any) {
        if (error.code !== "EEXIST") throw error;
        let info: fs.Stats;
        try { info = fs.lstatSync(lock); }
        catch (inspectionError: any) {
          // The owner released the lock after our mkdir saw EEXIST.
          if (inspectionError.code === "ENOENT") continue;
          throw inspectionError;
        }
        if (info.isSymbolicLink() || !info.isDirectory()) throw new Error("Unsafe token-store lock");
        if (performance.now() >= deadline) throw new Error("Token store is locked; fence the writer before recovery");
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20);
      }
    }
    try { return operation(); } finally { fs.rmdirSync(lock); }
  }

  private readAll(): Record<string, StoredUserToken> {
    let fd: number;
    try { fd = fs.openSync(this.path, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0)); }
    catch (error: any) { if (error.code === "ENOENT" && !fs.existsSync(this.path)) return {}; throw error; }
    let payload: any;
    try {
      const info = fs.fstatSync(fd);
      if (!info.isFile() || (info.mode & 0o777) !== 0o600) throw new Error("Token file requires mode 0600; explicit migration is needed");
      try { payload = JSON.parse(fs.readFileSync(fd, "utf-8")); }
      catch { throw new Error("Corrupt token store; original data was preserved"); }
    } finally { fs.closeSync(fd); }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("Corrupt token store; original data was preserved");
    const tokens = Object.prototype.hasOwnProperty.call(payload, "tokens") ? payload.tokens : payload;
    if (!tokens || typeof tokens !== "object" || Array.isArray(tokens)) throw new Error("Corrupt token store; original data was preserved");
    for (const item of Object.values(tokens) as any[]) {
      if (!item || typeof item !== "object" || ["app_id", "user_open_id", "access_token"].some(k => typeof item[k] !== "string")) throw new Error("Corrupt token record; original data was preserved");
    }
    return tokens;
  }

  private writeAll(tokens: Record<string, StoredUserToken>): void {
    const tempPath = `${this.path}.tmp.${randomUUID()}`;
    const fd = fs.openSync(tempPath, "wx", 0o600);
    try {
      fs.writeFileSync(fd, JSON.stringify({ tokens }, null, 2) + "\n", "utf-8");
      fs.fsyncSync(fd);
    } catch (error) { fs.closeSync(fd); fs.unlinkSync(tempPath); throw error; }
    fs.closeSync(fd);
    try {
      fs.renameSync(tempPath, this.path);
      const directory = fs.openSync(path.dirname(this.path), fs.constants.O_RDONLY);
      try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
    } finally { if (fs.existsSync(tempPath)) fs.unlinkSync(tempPath); }
  }

  load(app_id: string, user_open_id: string): StoredUserToken | null {
    const key = FileTokenStore.storageKey(app_id, user_open_id);
    const item = this.readAll()[key];
    if (!item) {
      return null;
    }
    return {
      app_id: String(item.app_id),
      user_open_id: String(item.user_open_id),
      access_token: String(item.access_token),
      refresh_token: item.refresh_token ?? null,
      expires_at: item.expires_at ?? null,
      refresh_expires_at: item.refresh_expires_at ?? null,
      scope: item.scope ?? null,
    };
  }

  save(token: StoredUserToken): StoredUserToken {
    return this.transaction(() => {
      const tokens = this.readAll();
      const key = FileTokenStore.storageKey(token.app_id, token.user_open_id);
      tokens[key] = { ...token };
      this.writeAll(tokens);
      return token;
    });
  }

  saveDeviceToken(
    app_id: string,
    user_open_id: string,
    token: DeviceToken,
    nowOrOptions?: number | { now?: number }
  ): StoredUserToken {
    let current: number;
    if (typeof nowOrOptions === "number") {
      current = nowOrOptions;
    } else {
      current = nowOrOptions?.now ?? Math.floor(Date.now() / 1000);
    }
    const stored: StoredUserToken = {
      app_id,
      user_open_id,
      access_token: token.access_token,
      refresh_token: token.refresh_token ?? null,
      expires_at: token.expires_in ? current + token.expires_in : null,
      refresh_expires_at: token.refresh_expires_in ? current + token.refresh_expires_in : null,
      scope: token.scope ?? null,
    };
    return this.save(stored);
  }

  remove(app_id: string, user_open_id: string): boolean {
    return this.transaction(() => {
      const tokens = this.readAll();
      const key = FileTokenStore.storageKey(app_id, user_open_id);
      if (!(key in tokens)) return false;
      delete tokens[key];
      this.writeAll(tokens);
      return true;
    });
  }

  status(app_id: string, user_open_id: string): TokenStatus {
    const current = this.load(app_id, user_open_id);
    if (!current) {
      return {
        app_id,
        user_open_id,
        exists: false,
        storage_path: this.path,
      };
    }
    return {
      app_id,
      user_open_id,
      exists: true,
      storage_path: this.path,
      scope: current.scope ?? null,
      expires_at: current.expires_at ?? null,
      refresh_expires_at: current.refresh_expires_at ?? null,
    };
  }
}
