import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { AgentEvent, AgentRunner, AgentTurnRequest, AgentTurnResult } from './agent-runtime.js';
import {
  CardPayload,
  actionElement,
  bindCardContinuation,
  cardButton,
  cardPayload,
  hrElement,
  mdDiv,
  noteElement,
} from './card-templates.js';
import {
  NativeCardAction,
  markRetryDelivered,
  markRetryFailed,
  resolveCardActionToRetry,
} from './native-contract.js';
import { FileContinuationStore, newOperationId } from './runtime-cards.js';

/* ------------------------------------------------------------------ */
/* 1. Zero-Token Suspend Protocol (`feishu-ask` / `agent-autoloop`)   */
/* ------------------------------------------------------------------ */

export interface FeishuAskDirective {
  title: string;
  text?: string;
  options: string[];
  key?: string;
  session?: string;
  cwd?: string;
}

export interface ParsedAutoloopOutput {
  summaryText: string;
  ask: FeishuAskDirective | null;
}

const ASK_BLOCK_REGEX = /```(?:feishu-ask|agent-autoloop)\s*\n([\s\S]*?)```\s*$/;

export function parseFeishuAskBlock(rawOutput: string): ParsedAutoloopOutput {
  const trimmed = (rawOutput || '').trim();
  const match = trimmed.match(ASK_BLOCK_REGEX);
  if (!match) {
    return { summaryText: trimmed, ask: null };
  }

  const summaryText = trimmed.slice(0, match.index).trim();
  const jsonBody = match[1].trim();
  try {
    const parsed = JSON.parse(jsonBody);
    const askNode = Array.isArray(parsed?.ask) ? parsed.ask[0] : parsed?.ask ?? parsed;
    if (
      askNode &&
      typeof askNode === 'object' &&
      typeof askNode.title === 'string' &&
      Array.isArray(askNode.options) &&
      askNode.options.length > 0
    ) {
      return {
        summaryText,
        ask: {
          title: String(askNode.title).trim(),
          text: askNode.text ? String(askNode.text).trim() : undefined,
          options: askNode.options.map((o: any) => String(o).trim()).filter(Boolean),
          key: askNode.key ? String(askNode.key) : undefined,
          session: askNode.session ? String(askNode.session) : undefined,
          cwd: askNode.cwd ? String(askNode.cwd) : undefined,
        },
      };
    }
  } catch {
    // Malformed JSON block: treat as plain output rather than crashing
  }
  return { summaryText: trimmed, ask: null };
}

export const AUTOLOOP_SYSTEM_PROMPT = [
  '你在后台自动化调度器 (Feishu Autoloop) 中运行。遵守以下铁律：',
  '1. 能自己通过代码、重试、换源解决的问题，直接改完验证并输出简明结论退出，严禁反问人类。',
  '2. 磁盘、进程、锁状态等基础检查由外层纯代码负责，不要为空白常规项生成多余决策。',
  '3. 只有遇到真正需要人类拍板的分叉（如：外部资源永久失效需选替代方案、高风险删除/变更、业务方向二选一），才在回复最末尾附一段 `feishu-ask` 代码块，格式如下：',
  '```feishu-ask',
  '{"title":"简明问题标题","text":"一句话背景说明","options":["方案A","方案B"]}',
  '```',
].join('\n');

/* ------------------------------------------------------------------ */
/* 2. Multi-Ammo Waterfall CLI Runner (Quota & Timeout Resilient)      */
/* ------------------------------------------------------------------ */

export interface AmmoRoute {
  id: string;
  label: string;
  /**
   * Command array where `{prompt}` is replaced by the prompt string.
   * If `{prompt}` is not present in args, prompt is passed as the final argument.
   */
  command: string[];
  cwd?: string;
  env?: Record<string, string>;
  timeoutSec?: number;
}

export interface AmmoCooldownRecord {
  routeId: string;
  reason: string;
  untilMs: number;
}

export const DEFAULT_AMMO_ROUTES: AmmoRoute[] = [
  {
    id: 'opencode-glm-flash',
    label: 'opencode (GLM-5.3-Flash)',
    command: ['opencode', 'run', '-m', 'zhipu-coding-plan/GLM-5.3-Flash', '{prompt}'],
    timeoutSec: 240,
  },
  {
    id: 'opencode-minimax-3.1-flash',
    label: 'opencode (MiniMax-M3.1-Flash)',
    command: ['opencode', 'run', '-m', 'minimax-cn-coding-plan/MiniMax-M3.1-Flash', '{prompt}'],
    timeoutSec: 240,
  },
  {
    id: 'opencode-glm-5.3',
    label: 'opencode (GLM-5.3)',
    command: ['opencode', 'run', '-m', 'zhipu-coding-plan/GLM-5.3', '{prompt}'],
    timeoutSec: 300,
  },
  {
    id: 'opencode-minimax-3.1',
    label: 'opencode (MiniMax-M3.1)',
    command: ['opencode', 'run', '-m', 'minimax-cn-coding-plan/MiniMax-M3.1', '{prompt}'],
    timeoutSec: 300,
  },
  {
    id: 'zcode',
    label: 'zcode',
    command: ['zcode', '-p', '{prompt}'],
    timeoutSec: 240,
  },
  {
    id: 'qoder',
    label: 'qoder',
    command: ['qodercli', '-p', '{prompt}'],
    timeoutSec: 240,
  },
  {
    id: 'cbc',
    label: 'cbc (CodeBuddy)',
    command: ['cbc', '-p', '{prompt}'],
    timeoutSec: 240,
  },
];

const QUOTA_OR_TRANSIENT_FAILURE_PATTERNS = [
  /429/i,
  /rate[\s_-]?limit/i,
  /quota/i,
  /insufficient/i,
  /balance/i,
  /err_[a-z0-9]+/i,
  /authentication\s+failed/i,
  /unauthorized/i,
  /timed?\s*out/i,
  /ECONNRESET|ETIMEDOUT|EAI_AGAIN/i,
];

export function isQuotaOrTransientFailure(
  returncode: number,
  stdout: string,
  stderr: string,
): { failed: boolean; reason: string } {
  const combined = `${stdout}\n${stderr}`.trim();
  if (returncode !== 0) {
    for (const pat of QUOTA_OR_TRANSIENT_FAILURE_PATTERNS) {
      if (pat.test(combined)) {
        return { failed: true, reason: `Matched failure pattern ${pat}` };
      }
    }
    return { failed: true, reason: `Exit code ${returncode}` };
  }
  if (!stdout.trim()) {
    return { failed: true, reason: 'Empty stdout from CLI' };
  }
  for (const pat of QUOTA_OR_TRANSIENT_FAILURE_PATTERNS) {
    if (pat.test(combined) && combined.length < 400) {
      return { failed: true, reason: `Short error output matched ${pat}` };
    }
  }
  return { failed: false, reason: 'ok' };
}

export class WaterfallCliRunner implements AgentRunner {
  readonly name = 'waterfall_cli';
  readonly routes: AmmoRoute[];
  readonly cooldownPath: string;
  readonly cooldownMs: number;
  readonly cwd: string | null;
  readonly spawnSyncFn: typeof spawnSync;
  readonly nowFn: () => number;

  constructor(options?: {
    routes?: AmmoRoute[];
    cooldownPath?: string;
    cooldownMs?: number;
    cwd?: string | null;
    spawnSyncFn?: typeof spawnSync;
    nowFn?: () => number;
  }) {
    this.routes =
      options?.routes && options.routes.length > 0 ? options.routes : DEFAULT_AMMO_ROUTES;
    this.cooldownPath =
      options?.cooldownPath ?? path.join(os.tmpdir(), 'feishu-autoloop-cooldowns.json');
    this.cooldownMs = options?.cooldownMs ?? 15 * 60 * 1000;
    this.cwd = options?.cwd ? path.resolve(options.cwd) : null;
    this.spawnSyncFn = options?.spawnSyncFn ?? spawnSync;
    this.nowFn = options?.nowFn ?? (() => Date.now());
  }

  loadCooldowns(): Record<string, AmmoCooldownRecord> {
    try {
      if (!fs.existsSync(this.cooldownPath)) return {};
      const data = JSON.parse(fs.readFileSync(this.cooldownPath, 'utf-8'));
      const now = this.nowFn();
      const active: Record<string, AmmoCooldownRecord> = {};
      for (const [k, v] of Object.entries(data || {})) {
        const rec = v as AmmoCooldownRecord;
        if (rec && typeof rec.untilMs === 'number' && rec.untilMs > now) {
          active[k] = rec;
        }
      }
      return active;
    } catch {
      return {};
    }
  }

  saveCooldown(routeId: string, reason: string): void {
    const current = this.loadCooldowns();
    current[routeId] = {
      routeId,
      reason,
      untilMs: this.nowFn() + this.cooldownMs,
    };
    try {
      fs.mkdirSync(path.dirname(this.cooldownPath), { recursive: true });
      fs.writeFileSync(this.cooldownPath, JSON.stringify(current, null, 2), 'utf-8');
    } catch {
      // ignore write errors on read-only fs
    }
  }

  private buildFullPrompt(request: AgentTurnRequest): string {
    const sys = request.systemPrompt
      ? `${AUTOLOOP_SYSTEM_PROMPT}\n\n${request.systemPrompt}`
      : AUTOLOOP_SYSTEM_PROMPT;
    return `${sys}\n\n任务指令：\n${request.prompt}`.trim();
  }

  run(request: AgentTurnRequest): AgentTurnResult {
    const fullPrompt = this.buildFullPrompt(request);
    const cooldowns = this.loadCooldowns();
    const events: AgentEvent[] = [AgentEvent.start('Waterfall CLI runner started')];
    const attempts: Array<{ routeId: string; label: string; reason: string }> = [];

    // Order routes: non-cooled-down first; if all are in cooldown, still try them in order as degraded fallback
    const available = this.routes.filter((r) => !cooldowns[r.id]);
    const candidateRoutes = available.length > 0 ? available : this.routes;

    for (const route of candidateRoutes) {
      const cmdBin = route.command[0];
      const rawArgs = route.command.slice(1);
      const hasPlaceholder = rawArgs.some((a) => a.includes('{prompt}'));
      const args = hasPlaceholder
        ? rawArgs.map((a) => a.replace('{prompt}', fullPrompt))
        : [...rawArgs, fullPrompt];

      events.push(
        AgentEvent.toolCall('cli.exec', { route: route.id, label: route.label }, {
          detail: `${route.label} (${cmdBin})`,
        }),
      );

      const res = this.spawnSyncFn(cmdBin, args, {
        encoding: 'utf-8',
        timeout: (route.timeoutSec ?? 240) * 1000,
        cwd: route.cwd || this.cwd || undefined,
        env: { ...process.env, ...(route.env || {}) },
      });

      const stdout = String(res.stdout || '').trim();
      const stderr = String(res.stderr || '').trim();
      const returncode = res.status ?? (res.error ? 1 : 0);

      const check = isQuotaOrTransientFailure(returncode, stdout, stderr);
      if (!check.failed) {
        const parsed = parseFeishuAskBlock(stdout);
        events.push(
          AgentEvent.toolResult('cli.exec', `Succeeded via ${route.label}`, {
            route_id: route.id,
            label: route.label,
          }),
        );
        events.push(AgentEvent.assistantMessage(parsed.summaryText || stdout));
        events.push(
          AgentEvent.completed(`Completed via ${route.label}`, {
            route_id: route.id,
            has_ask: Boolean(parsed.ask),
          }),
        );
        return new AgentTurnResult({
          runner: this.name,
          outputText: stdout,
          status: parsed.ask ? 'suspended_ask' : 'completed',
          events,
          metadata: {
            selected_route: route.id,
            selected_label: route.label,
            attempts,
            summary_text: parsed.summaryText,
            ask: parsed.ask,
          },
        });
      }

      // Failed: record cooldown and fall back to next route immediately
      this.saveCooldown(route.id, check.reason);
      attempts.push({ routeId: route.id, label: route.label, reason: check.reason });
      events.push(
        AgentEvent.stderrWarning(`Route ${route.label} failed (${check.reason}), switching ammo...`, {
          route_id: route.id,
          reason: check.reason,
        }),
      );
    }

    const failSummary = attempts.map((a) => `${a.label}: ${a.reason}`).join(' | ');
    events.push(
      AgentEvent.error('All CLI ammo routes failed', {
        detail: failSummary,
        metadata: { attempts },
      }),
    );

    return new AgentTurnResult({
      runner: this.name,
      outputText: '',
      status: 'error',
      events,
      metadata: {
        selected_route: null,
        attempts,
      },
    });
  }
}

/* ------------------------------------------------------------------ */
/* 3. Human-in-the-Loop Card Builder & Suspend/Resume State Machine    */
/* ------------------------------------------------------------------ */

export function buildAutoloopAskCard(options: {
  operationId: string;
  botName?: string;
  nodeName?: string;
  summaryText?: string;
  ask: FeishuAskDirective;
  selectedOption?: string | null;
  decidedBy?: string | null;
}): CardPayload {
  const headerPrefix = options.botName
    ? `[${options.botName}] `
    : options.nodeName
      ? `[${options.nodeName}] `
      : '';
  const elements: Array<Record<string, any>> = [];

  if (options.summaryText) {
    elements.push(mdDiv(`**阶段进展小结：**\n${options.summaryText}`), hrElement());
  }

  if (options.ask.text) {
    elements.push(mdDiv(options.ask.text));
  }

  if (!options.selectedOption) {
    elements.push(
      mdDiv('**遇到需要你拍板的分叉（等待期间零进程、零 Token 消耗）：**'),
      actionElement(
        options.ask.options.map((opt, idx) =>
          cardButton(`${idx + 1}. ${opt}`, `autoloop_choose_${idx}`, {
            buttonType: idx === 0 ? 'primary' : 'default',
            operationId: options.operationId,
            value: {
              choice_index: idx,
              choice_text: opt,
            },
          }),
        ),
      ),
      noteElement(`operation_id: ${options.operationId} · 点击按钮或回复序号即可接续原工作区`),
    );
    return cardPayload({
      title: `🤔 ${headerPrefix}${options.ask.title}`,
      color: 'yellow',
      elements,
    });
  }

  const byText = options.decidedBy ? `（由 ${options.decidedBy} 拍板）` : '';
  elements.push(
    hrElement(),
    mdDiv(`✅ **已拍板选择：** \`${options.selectedOption}\` ${byText}`),
    noteElement(`operation_id: ${options.operationId} · 正在唤醒原工作区继续执行`),
  );
  return cardPayload({
    title: `✅ ${headerPrefix}${options.ask.title}（已拍板）`,
    color: 'green',
    elements,
  });
}

export function suspendAutoloopAsk(
  store: FileContinuationStore,
  options: {
    appId: string;
    operationId?: string;
    ask: FeishuAskDirective;
    summaryText: string;
    originalPrompt: string;
    cwd?: string | null;
    sessionId?: string | null;
    botName?: string;
    nodeName?: string;
  },
): { operationId: string; card: CardPayload } {
  const operationId = options.operationId ?? newOperationId();
  bindCardContinuation(store, {
    operationId,
    appId: options.appId,
    continuationKind: 'autoloop_ask',
    retryText: options.originalPrompt,
    metadata: {
      ask: options.ask,
      summary_text: options.summaryText,
      original_prompt: options.originalPrompt,
      cwd: options.cwd ?? options.ask.cwd ?? null,
      session_id: options.sessionId ?? options.ask.session ?? null,
      bot_name: options.botName ?? null,
      node_name: options.nodeName ?? null,
    },
  });

  const card = buildAutoloopAskCard({
    operationId,
    botName: options.botName,
    nodeName: options.nodeName,
    summaryText: options.summaryText,
    ask: options.ask,
  });

  return { operationId, card };
}

export function answerAutoloopAsk(
  store: FileContinuationStore,
  runner: WaterfallCliRunner,
  options: {
    operationId: string;
    choice: string | number;
    actorOpenId?: string | null;
  },
): {
  duplicate: boolean;
  chosenOption: string;
  updatedCard: CardPayload;
  turnResult: AgentTurnResult | null;
} {
  const existing = store.load(options.operationId);
  if (!existing) {
    throw new Error(`Continuation not found for operation_id: ${options.operationId}`);
  }
  const meta = existing.payload?.metadata || existing.payload || {};
  const ask: FeishuAskDirective = meta.ask || {
    title: '人工拍板',
    options: [String(options.choice)],
  };

  let chosenOption = String(options.choice);
  const numVal =
    typeof options.choice === 'number'
      ? options.choice
      : /^\d+$/.test(String(options.choice).trim())
        ? Number.parseInt(String(options.choice).trim(), 10)
        : -1;
  const idx = numVal >= 1 ? numVal - 1 : numVal === 0 ? 0 : -1;
  if (idx >= 0 && idx < ask.options.length) {
    chosenOption = ask.options[idx];
  }

  const cardAction = new NativeCardAction({
    operationId: options.operationId,
    action: `autoloop_choose:${chosenOption}`,
    actorOpenId: options.actorOpenId ?? null,
  });

  const resolution = resolveCardActionToRetry(cardAction, store);
  const updatedCard = buildAutoloopAskCard({
    operationId: options.operationId,
    botName: meta.bot_name,
    nodeName: meta.node_name,
    summaryText: meta.summary_text,
    ask,
    selectedOption: chosenOption,
    decidedBy: options.actorOpenId ?? null,
  });

  if (resolution.duplicate) {
    return {
      duplicate: true,
      chosenOption,
      updatedCard,
      turnResult: null,
    };
  }

  const resumePrompt = [
    `原任务：${meta.original_prompt || resolution.retryRequest.text}`,
    meta.summary_text ? `上一轮已完成进展：${meta.summary_text}` : '',
    `针对问题「${ask.title}」，人类已拍板选择：「${chosenOption}」。`,
    '请直接按该决定在原目录把剩余工作执行到底，完成后输出最终结论。',
  ]
    .filter(Boolean)
    .join('\n\n');

  const dummyContext: any = {
    eventType: 'autoloop.resume',
    messageId: options.operationId,
    chatId: null,
    senderOpenId: options.actorOpenId ?? null,
    promptText: () => resumePrompt,
    toDict: () => ({ event_type: 'autoloop.resume', operation_id: options.operationId }),
  };

  const turnReq = new AgentTurnRequest({
    context: dummyContext,
    prompt: resumePrompt,
    sessionId: meta.session_id ?? null,
    metadata: { cwd: meta.cwd ?? null },
  });

  const turnResult = runner.run(turnReq);
  if (turnResult.status === 'error') {
    markRetryFailed(store, options.operationId, { reason: 'All ammo routes failed on resume' });
  } else {
    markRetryDelivered(store, options.operationId);
  }

  return {
    duplicate: false,
    chosenOption,
    updatedCard,
    turnResult,
  };
}

/* ------------------------------------------------------------------ */
/* 4. Pure-Code Fleet & Process Board (`buildFleetBoardCard`)          */
/*    0 Token, Never Calls LLM for Disk/Process/Lane Status!           */
/* ------------------------------------------------------------------ */

export interface ActiveLaneProcess {
  lane: string;
  slug: string;
  pid: number;
  elapsed: string;
  ammo: string;
  node?: string;
}

export interface FleetNodeStatus {
  node: string;
  botName: string;
  role: string;
  online: boolean;
  diskPct: string;
  loadAvg: string;
  activeLanes: ActiveLaneProcess[];
  tmuxSessions?: string[];
  defaultAmmo: string;
  note?: string;
}

export interface FleetSnapshot {
  updatedAt: string;
  headCommit?: string;
  recentMerged?: string[];
  pendingLanes?: number;
  suspendedAsks?: Array<{ operationId: string; title: string; lane?: string }>;
  nodes: FleetNodeStatus[];
}

export function buildFleetBoardCard(snapshot: FleetSnapshot): CardPayload {
  const elements: Array<Record<string, any>> = [];

  const summaryParts: string[] = [`🕒 **更新时间**：\`${snapshot.updatedAt}\``];
  if (snapshot.headCommit) {
    summaryParts.push(`📦 **主干 HEAD**：\`${snapshot.headCommit}\``);
  }
  if (typeof snapshot.pendingLanes === 'number') {
    summaryParts.push(`📥 **待命车道**：\`${snapshot.pendingLanes}\``);
  }
  elements.push(mdDiv(summaryParts.join('  |  ')));

  if (snapshot.recentMerged && snapshot.recentMerged.length > 0) {
    elements.push(mdDiv(`🏆 **最近合入**：${snapshot.recentMerged.join(' → ')}`));
  }

  elements.push(hrElement());

  for (const n of snapshot.nodes) {
    const badge = n.online ? '🟢' : '🔴';
    const headerLine = `${badge} **${n.botName} (\`${n.node}\`)** · ${n.role}  |  💾 磁盘 \`${n.diskPct}\`  |  ⚡ 负载 \`${n.loadAvg}\`  |  🔫 主力 \`${n.defaultAmmo}\``;
    const detailLines: string[] = [headerLine];

    if (n.activeLanes && n.activeLanes.length > 0) {
      for (const l of n.activeLanes) {
        detailLines.push(
          `   └─ 🚜 **${l.lane}** (\`${l.slug}\`) · \`PID ${l.pid}\` · 已跑 \`${l.elapsed}\` · 弹药 \`${l.ammo}\``,
        );
      }
    } else if (n.tmuxSessions && n.tmuxSessions.length > 0) {
      detailLines.push(`   └─ 🖥️ 常驻会话：\`${n.tmuxSessions.join(', ')}\`（当前无在跑车道）`);
    } else {
      detailLines.push('   └─ ⚪ 空闲待命');
    }

    if (n.note) {
      detailLines.push(`   └─ 💡 ${n.note}`);
    }
    elements.push(mdDiv(detailLines.join('\n')));
  }

  if (snapshot.suspendedAsks && snapshot.suspendedAsks.length > 0) {
    elements.push(hrElement(), mdDiv('⏸️ **当前挂起等待拍板的事项（0 Token 等待中）：**'));
    for (const item of snapshot.suspendedAsks) {
      elements.push(mdDiv(`• \`${item.operationId}\`：${item.title}`));
    }
  }

  elements.push(
    hrElement(),
    actionElement([
      cardButton('🔄 刷新车队看板', 'fleet_refresh', {
        buttonType: 'primary',
        operationId: 'fleet_board',
      }),
      cardButton('🧹 清理超时锁并唤醒', 'fleet_heal_locks', {
        buttonType: 'default',
        operationId: 'fleet_board',
      }),
    ]),
    noteElement('纯代码直采看板（0 Token 消耗）· 常规体检与自愈由代码自动完成，拿不准才发卡找你拍板'),
  );

  return cardPayload({
    title: '🚜 奥创军团 · 车队与进程实时看板',
    color: 'turquoise',
    elements,
  });
}
