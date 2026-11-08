/**
 * Zero-network local simulator for Feishu CardKit interactive cards.
 *
 * Renders CardKit 2.0 JSON payloads as ANSI terminal output, synthesizes
 * `card.action.trigger` callback events from simulated clicks, and routes them
 * through `InteractiveActionDispatcher` — closing the develop-test loop without
 * a webhook, a public network, or a real Feishu client. Also ships demo wiring
 * that drives the full action -> continuation -> retry contract for each
 * built-in template.
 */

import readline from 'node:readline';
import {
  CARD_TEMPLATE_NAMES,
  CardPayload,
  CardTemplateName,
  buildDemoCardPayload,
  bindCardContinuation,
  isCardTemplateName,
  MultiSelectCardController,
  AgentPlanCardController,
  ProgressCardController,
  TypewriterCardTemplate,
  ErrorRetryCardController,
} from './card-templates.js';
import { InteractiveActionDispatcher, CardActionResponse } from './interactive-dispatcher.js';
import {
  NativeCardAction,
  markRetryDelivered,
  markRetryFailed,
  resolveCardActionToRetry,
} from './native-contract.js';
import { FileContinuationStore } from './runtime-cards.js';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';

/* ------------------------------------------------------------------ */
/* ANSI + markdown rendering                                           */
/* ------------------------------------------------------------------ */

const ANSI = {
  reset: '\x1b[0m',
  bold: '\x1b[1m',
  dim: '\x1b[2m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m',
  cyan: '\x1b[36m',
  grey: '\x1b[90m',
} as const;

type AnsiColor = keyof typeof ANSI;

const HEADER_COLORS: Record<string, AnsiColor> = {
  blue: 'blue',
  green: 'green',
  red: 'red',
  grey: 'grey',
  gray: 'grey',
  yellow: 'yellow',
  turquoise: 'cyan',
};

const TOAST_COLORS: Record<string, AnsiColor> = {
  info: 'blue',
  success: 'green',
  warning: 'yellow',
  danger: 'red',
};

export function envSupportsColor(stream: { isTTY?: boolean } | undefined = process.stdout): boolean {
  if (process.env.NO_COLOR) return false;
  if (process.env.FORCE_COLOR && process.env.FORCE_COLOR !== '0') return true;
  return Boolean(stream?.isTTY);
}

function wrap(text: string, code: AnsiColor | null, color: boolean): string {
  if (!color || !code) return text;
  return `${ANSI[code]}${text}${ANSI.reset}`;
}

/** Convert the lark_md subset emitted by this kit into ANSI/plain text. */
export function larkMdToText(content: string, color: boolean): string {
  let text = content;
  text = text.replace(
    /<font color="([a-z]+)">([\s\S]*?)<\/font>/g,
    (_match, fontColor: string, inner: string) => {
      const code = (fontColor === 'grey' || fontColor === 'gray'
        ? 'grey'
        : (fontColor as AnsiColor)) as AnsiColor;
      return wrap(inner, ANSI[code] ? code : null, color);
    },
  );
  text = text.replace(
    /<span data-tag="(gray|grey)">([\s\S]*?)<\/span>/g,
    (_match, _tag: string, inner: string) => wrap(inner, 'dim', color),
  );
  text = text.replace(/`([^`]+)`/g, (_match, inner: string) => wrap(inner, 'cyan', color));
  text = text.replace(/\*\*([^*]+)\*\*/g, (_match, inner: string) => wrap(inner, 'bold', color));
  text = text.replace(/<[^>]+>/g, '');
  return text
    .split('\n')
    .map((line) =>
      line.startsWith('> ') ? `${wrap(`▎${line.slice(2)}`, 'dim', color)}` : line,
    )
    .join('\n');
}

export interface CardRenderOptions {
  color?: boolean;
  width?: number;
  /** Highlight this clickable action index (interactive selection cursor). */
  selectedAction?: number | null;
}

export interface CardActionRef {
  index: number;
  label: string;
  buttonType: string;
  value: Record<string, any>;
}

/** All clickable buttons across the card's action elements, in click order. */
export function collectCardActions(payload: Record<string, any>): CardActionRef[] {
  const actions: CardActionRef[] = [];
  const elements = Array.isArray(payload?.elements) ? payload.elements : [];
  for (const element of elements) {
    if (element?.tag !== 'action' || !Array.isArray(element.actions)) continue;
    for (const button of element.actions) {
      if (button?.tag !== 'button') continue;
      actions.push({
        index: actions.length,
        label: String(button.text?.content ?? button.text ?? 'button'),
        buttonType: String(button.type ?? 'default'),
        value: button.value ?? {},
      });
    }
  }
  return actions;
}

function horizontalRule(width: number): string {
  return '─'.repeat(width);
}

function displayWidth(text: string): number {
  // Rough CJK-aware width so rules visually align; good enough for a terminal tool.
  let width = 0;
  for (const char of text) {
    width += /[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE30-\uFE4F\uFF00-\uFF60\uFFE0-\uFFE6\u3000-\u303F\u4E00-\u9FFF]/.test(
      char,
    )
      ? 2
      : 1;
  }
  return width;
}

/**
 * Render a CardKit 2.0 payload (as built by streaming-card.ts and
 * card-templates.ts) to terminal text. Covers the element subset the kit
 * emits — div/lark_md, hr, action buttons, note, img placeholder — and falls
 * back to a compact JSON line for anything else.
 */
export function renderCardJson(
  payload: Record<string, any>,
  options: CardRenderOptions = {},
): string {
  const color = options.color ?? false;
  const width = options.width ?? 72;
  const selected = options.selectedAction ?? null;
  const lines: string[] = [];

  const header = payload?.header ?? {};
  const title = String(header.title?.content ?? header.title ?? 'Card');
  const templateColor = String(header.template ?? 'blue');
  const headerCode = HEADER_COLORS[templateColor] ?? 'blue';
  const headerText = `▌ ${title}`;
  lines.push(wrap(headerText, headerCode, color));
  const pad = Math.max(0, width - displayWidth(wrap(headerText, null, false)) - 2);
  lines.push(wrap(horizontalRule(Math.min(width, displayWidth(wrap(headerText, null, false)) + pad)), 'dim', color));

  const elements = Array.isArray(payload?.elements) ? payload.elements : [];
  for (const element of elements) {
    if (!element || typeof element !== 'object') continue;
    switch (element.tag) {
      case 'div': {
        const text = element.text?.content ?? element.text ?? '';
        lines.push(larkMdToText(String(text), color));
        break;
      }
      case 'hr':
        lines.push(wrap(horizontalRule(Math.max(8, Math.floor(width * 0.6))), 'dim', color));
        break;
      case 'action': {
        const buttons = collectCardActions({ elements: [element] });
        const rendered = buttons.map((button) => {
          const marker = button.index === selected ? wrap('▶', 'yellow', color) : ' ';
          const label = `[${button.index + 1}] ${button.label}`;
          const code: AnsiColor =
            button.buttonType === 'primary' ? 'green' : button.buttonType === 'danger' ? 'red' : 'blue';
          return `${marker}${wrap(label, code, color)}`;
        });
        lines.push(rendered.join('   '));
        break;
      }
      case 'note': {
        const parts = (element.elements ?? [])
          .map((note: any) => String(note?.content ?? note?.text ?? ''))
          .filter(Boolean);
        if (parts.length) {
          lines.push(wrap(`· ${parts.join(' · ')}`, 'dim', color));
        }
        break;
      }
      case 'img':
        lines.push(wrap(`[图片: ${element.alt?.content ?? element.img_key ?? 'image'}]`, 'dim', color));
        break;
      default:
        lines.push(wrap(`(unsupported element: ${element.tag})`, 'dim', color));
    }
  }

  lines.push(wrap(horizontalRule(width), 'dim', color));
  return lines.join('\n');
}

export function renderToast(toast: { type: string; content: string }, color: boolean): string {
  const code = TOAST_COLORS[toast.type] ?? 'blue';
  return wrap(`toast[${toast.type}] ${toast.content}`, code, color);
}

/* ------------------------------------------------------------------ */
/* Click simulation                                                    */
/* ------------------------------------------------------------------ */

export function buildActionTriggerEvent(options: {
  payload: Record<string, any>;
  value: Record<string, any>;
  actorOpenId?: string | null;
  messageId?: string | null;
  chatId?: string | null;
}): Record<string, any> {
  const messageId = options.messageId ?? 'om_simulator_1';
  const chatId = options.chatId ?? 'oc_simulator_1';
  const actorOpenId = options.actorOpenId ?? 'ou_simulator';
  return {
    schema: 'feishu-auth-kit.simulated-card-action.v1',
    ts: Date.now(),
    event: {
      type: 'card_action_trigger',
      operator: { open_id: actorOpenId },
      context: { open_message_id: messageId, open_chat_id: chatId },
      action: {
        tag: 'button',
        value: { ...options.value },
      },
    },
  };
}

export interface CardSimulatorOptions {
  payload: Record<string, any>;
  dispatcher?: InteractiveActionDispatcher;
  actorOpenId?: string;
  messageId?: string;
  chatId?: string;
  color?: boolean;
  width?: number;
  output?: (line: string) => void;
}

export class CardSimulator {
  payload: Record<string, any>;
  readonly dispatcher: InteractiveActionDispatcher;
  readonly colorEnabled: boolean;
  private readonly actorOpenId: string;
  private readonly messageId: string;
  private readonly chatId: string;
  private readonly color: boolean;
  private readonly width: number;
  private readonly output: (line: string) => void;

  constructor(options: CardSimulatorOptions) {
    this.payload = options.payload;
    this.dispatcher = options.dispatcher ?? new InteractiveActionDispatcher();
    this.actorOpenId = options.actorOpenId ?? 'ou_simulator';
    this.messageId = options.messageId ?? 'om_simulator_1';
    this.chatId = options.chatId ?? 'oc_simulator_1';
    this.color = options.color ?? envSupportsColor();
    this.colorEnabled = this.color;
    this.width = options.width ?? 72;
    this.output = options.output ?? ((line: string) => process.stdout.write(`${line}\n`));
  }

  render(options: CardRenderOptions = {}): string {
    return renderCardJson(this.payload, {
      color: options.color ?? this.color,
      width: options.width ?? this.width,
      selectedAction: options.selectedAction,
    });
  }

  actions(): CardActionRef[] {
    return collectCardActions(this.payload);
  }

  print(options: CardRenderOptions = {}): void {
    this.output(this.render(options));
  }

  /**
   * Simulate a click on the action at `actionIndex` (index within the current
   * payload — in-place card updates can change the button list).
   */
  async click(actionIndex: number): Promise<CardActionResponse> {
    const actions = this.actions();
    const ref = actions[actionIndex];
    if (!ref) {
      throw new Error(`no clickable action at index ${actionIndex} (card has ${actions.length})`);
    }
    const event = buildActionTriggerEvent({
      payload: this.payload,
      value: ref.value,
      actorOpenId: this.actorOpenId,
      messageId: this.messageId,
      chatId: this.chatId,
    });
    const response = await this.dispatcher.dispatch(event);
    if (response.card) {
      this.payload = response.card;
    }
    if (response.toast) {
      this.output(renderToast(response.toast, this.color));
    }
    return response;
  }

  /**
   * Simulate a double-click / callback race: `times` identical callbacks all
   * dispatch against the same pre-click payload (as when Feishu delivers both
   * clicks before the card update lands). The first card response wins; the
   * rest exercise the idempotency guard.
   */
  async clickRepeated(actionIndex: number, times: number): Promise<CardActionResponse[]> {
    if (!Number.isInteger(times) || times < 1) {
      throw new Error(`clickRepeated: times must be a positive integer, got ${times}`);
    }
    const stalePayload = this.payload;
    const ref = collectCardActions(stalePayload)[actionIndex];
    if (!ref) {
      throw new Error(`no clickable action at index ${actionIndex} (card has ${collectCardActions(stalePayload).length})`);
    }
    const responses: CardActionResponse[] = [];
    for (let i = 0; i < times; i++) {
      const event = buildActionTriggerEvent({
        payload: stalePayload,
        value: ref.value,
        actorOpenId: this.actorOpenId,
        messageId: this.messageId,
        chatId: this.chatId,
      });
      responses.push(await this.dispatcher.dispatch(event));
    }
    const applied = responses.find((response) => response.card);
    if (applied?.card) {
      this.payload = applied.card;
    }
    for (const response of responses) {
      if (response.toast) {
        this.output(renderToast(response.toast, this.color));
      }
    }
    return responses;
  }
}

/* ------------------------------------------------------------------ */
/* Interactive keypress loop                                           */
/* ------------------------------------------------------------------ */

export interface SimulationLoopOptions {
  stdin?: NodeJS.ReadStream & { isTTY?: boolean };
  output?: (line: string) => void;
}

/**
 * Key-driven loop: ↑/↓ (or j/k) select a button, Enter/Space clicks it through
 * the dispatcher, `r` redraws, `q`/Ctrl-C exits. Falls back to a single render
 * when stdin is not a TTY (CI, pipes).
 */
export async function runCardSimulationLoop(
  simulator: CardSimulator,
  options: SimulationLoopOptions = {},
): Promise<void> {
  const stdin = options.stdin ?? (process.stdin as NodeJS.ReadStream & { isTTY?: boolean });
  const output = options.output ?? ((line: string) => process.stdout.write(`${line}\n`));

  if (!stdin.isTTY) {
    simulator.print();
    return;
  }

  let selected = 0;
  let busy = false;
  let closed = false;

  const redraw = () => {
    output('');
    simulator.print({ selectedAction: selected });
    const count = simulator.actions().length;
    output(
      count > 0
        ? '↑/↓ 或 j/k 选择按钮 · Enter/Space 点击 · r 重绘 · q 退出'
        : '卡片没有可点击按钮 · r 重绘 · q 退出',
    );
  };

  readline.emitKeypressEvents(stdin);
  if (typeof stdin.setRawMode === 'function') {
    stdin.setRawMode(true);
  }
  stdin.resume();

  await new Promise<void>((resolve) => {
    const cleanup = () => {
      if (closed) return;
      closed = true;
      stdin.removeListener('keypress', onKeypress);
      if (typeof stdin.setRawMode === 'function') {
        try {
          stdin.setRawMode(false);
        } catch {
          /* stream already closing */
        }
      }
      stdin.pause();
      resolve();
    };

    const onKeypress = async (_chunk: unknown, key: { name?: string; ctrl?: boolean } = {}) => {
      if (closed) return;
      if (busy) return;
      const name = key?.name;
      const count = simulator.actions().length;

      if (name === 'q' || name === 'escape' || (key?.ctrl && name === 'c')) {
        output(wrap('已退出模拟。', 'dim', simulator.colorEnabled));
        cleanup();
        return;
      }
      if (name === 'r') {
        redraw();
        return;
      }
      if (count === 0) return;

      if (name === 'up' || name === 'k') {
        selected = (selected - 1 + count) % count;
        redraw();
        return;
      }
      if (name === 'down' || name === 'j') {
        selected = (selected + 1) % count;
        redraw();
        return;
      }
      if (name === 'return' || name === 'enter' || name === 'space') {
        busy = true;
        try {
          await simulator.click(selected);
          redraw();
        } finally {
          busy = false;
        }
      }
    };

    stdin.on('keypress', onKeypress);
    stdin.on('close', cleanup);
    redraw();
  });
}

/* ------------------------------------------------------------------ */
/* Template demo wiring (drives the native contract end to end)         */
/* ------------------------------------------------------------------ */

export function defaultSimulationStorePath(): string {
  const configured = process.env.FEISHU_AUTH_KIT_SIMULATION_STORE;
  if (configured) {
    return path.resolve(configured);
  }
  return path.join(os.tmpdir(), 'feishu-auth-kit-card-simulator', 'continuations.json');
}

export interface TemplateSimulation {
  templateName: CardTemplateName;
  payload: Record<string, any>;
  dispatcher: InteractiveActionDispatcher;
  controller:
    | AgentPlanCardController
    | TypewriterCardTemplate
    | ProgressCardController
    | MultiSelectCardController
    | ErrorRetryCardController;
  store: FileContinuationStore;
}

export interface TemplateSimulationOptions {
  storePath?: string | null;
  /** Simulate a failing card-update delivery to exercise timeout recovery. */
  deliveryFailure?: boolean;
  freshStore?: boolean;
}

function nativeAction(
  operationId: string,
  action: string,
  actorOpenId: string,
  value: Record<string, any>,
): NativeCardAction {
  return new NativeCardAction({
    operationId,
    action,
    actorOpenId,
    messageId: 'om_simulator_1',
    payload: { operation_id: operationId, ...value },
  });
}

/**
 * Build a demo simulation for a built-in template: payload + controller +
 * dispatcher whose handlers run the real continuation contract, including
 * duplicate-click suppression and delivery failure recovery.
 */
export function createTemplateSimulation(
  name: string,
  options: TemplateSimulationOptions = {},
): TemplateSimulation {
  if (!isCardTemplateName(name)) {
    throw new Error(`unknown template: ${name} (expected one of ${CARD_TEMPLATE_NAMES.join(', ')})`);
  }
  const storePath = options.storePath === undefined ? defaultSimulationStorePath() : options.storePath;
  const store = new FileContinuationStore(storePath ?? undefined);
  if (options.freshStore && storePath && fs.existsSync(storePath)) {
    fs.rmSync(storePath, { force: true });
  }
  const dispatcher = new InteractiveActionDispatcher();

  switch (name) {
    case 'plan_confirmation': {
      const controller = new AgentPlanCardController({
        appId: 'cli_demo',
        operationId: 'sim-plan-0001',
        steps: [
          { title: '拉取仓库最新代码', detail: 'git pull --rebase origin main' },
          { title: '运行单测与类型检查', detail: 'pnpm test && pnpm typecheck' },
          { title: '构建并输出产物摘要' },
        ],
      });
      bindCardContinuation(store, {
        operationId: controller.operationId,
        appId: controller.appId,
        continuationKind: 'plan_confirmation',
        retryText: controller.buildRetryText(),
      });
      const resolve = (action: string, ctxValue: Record<string, any>, actor: string) =>
        resolveCardActionToRetry(nativeAction(controller.operationId, action, actor, ctxValue), store);

      dispatcher.onAction('plan_approve', (ctx) => {
        const resolved = resolve('plan_approve', ctx.actionValue, ctx.openId);
        if (resolved.duplicate) {
          return {
            toast: { type: 'warning', content: '该计划已处理过（幂等保护），重复点击已忽略。' },
          };
        }
        if (options.deliveryFailure) {
          markRetryFailed(store, controller.operationId, { reason: 'simulated delivery timeout' });
          return {
            toast: { type: 'danger', content: '卡片更新投递失败，continuation 已回滚到 waiting，可重新点击。' },
          };
        }
        markRetryDelivered(store, controller.operationId);
        controller.approve(ctx.openId);
        return {
          toast: { type: 'success', content: '已批准：synthetic retry 已生成并投递。' },
          card: controller.buildCardPayload() as Record<string, any>,
        };
      });
      dispatcher.onAction('plan_reject', (ctx) => {
        const resolved = resolve('plan_reject', ctx.actionValue, ctx.openId);
        if (resolved.duplicate) {
          return {
            toast: { type: 'warning', content: '该计划已处理过（幂等保护），重复点击已忽略。' },
          };
        }
        markRetryDelivered(store, controller.operationId);
        controller.reject(ctx.openId);
        return {
          toast: { type: 'success', content: '已驳回：流程终止，continuation 已关闭。' },
          card: controller.buildCardPayload() as Record<string, any>,
        };
      });
      return {
        templateName: name,
        payload: controller.buildCardPayload() as Record<string, any>,
        dispatcher,
        controller,
        store,
      };
    }

    case 'typewriter': {
      const controller = new TypewriterCardTemplate({
        operationId: 'sim-typewriter-0001',
        initialText: '正在阅读 `src/card-templates.ts`…\n\n已定位 3 个可优化的分发分支，正在生成重构建议。',
      });
      return {
        templateName: name,
        payload: controller.buildCardPayload() as Record<string, any>,
        dispatcher,
        controller,
        store,
      };
    }

    case 'progress_tracking': {
      const controller = new ProgressCardController({
        appId: 'cli_demo',
        operationId: 'sim-progress-0001',
        steps: ['环境检查', '依赖安装', '测试执行', '构建产物'],
      });
      controller.start();
      controller.completeStep('step-1', 'node 26 / pnpm 10 就绪');
      controller.start();
      bindCardContinuation(store, {
        operationId: controller.operationId,
        appId: controller.appId,
        continuationKind: 'progress_tracking',
        retryText: '继续执行被取消的任务',
      });
      dispatcher.onAction('progress_cancel', (ctx) => {
        const resolved = resolveCardActionToRetry(
          nativeAction(controller.operationId, 'progress_cancel', ctx.openId, ctx.actionValue),
          store,
        );
        if (resolved.duplicate) {
          return { toast: { type: 'warning', content: '任务已取消过（幂等保护）。' } };
        }
        markRetryDelivered(store, controller.operationId);
        controller.cancel(ctx.openId);
        return {
          toast: { type: 'success', content: '任务已取消。' },
          card: controller.buildCardPayload() as Record<string, any>,
        };
      });
      return {
        templateName: name,
        payload: controller.buildCardPayload() as Record<string, any>,
        dispatcher,
        controller,
        store,
      };
    }

    case 'multi_select': {
      const controller = new MultiSelectCardController({
        appId: 'cli_demo',
        operationId: 'sim-multiselect-0001',
        question: '本次发布要包含哪些变更？',
        options: [
          { value: 'runtime', label: '运行时内核收敛' },
          { value: 'cards', label: '卡片模板库' },
          { value: 'docs', label: '文档与决策记录' },
          { value: 'ci', label: 'CI 升级' },
        ],
        minSelect: 1,
      });
      dispatcher.onAction('multi_select_toggle', (ctx) => {
        const option = String(ctx.actionValue?.option ?? '');
        const nowSelected = controller.toggle(option);
        return {
          toast: {
            type: 'info',
            content: nowSelected ? `已勾选：${option}` : `已取消勾选：${option}`,
          },
          card: controller.buildCardPayload() as Record<string, any>,
        };
      });
      dispatcher.onAction('multi_select_confirm', (ctx) => {
        const selection = controller.confirm(ctx.openId);
        if (selection === null) {
          return { toast: { type: 'warning', content: '至少选择 1 项，或该决策已提交。' } };
        }
        bindCardContinuation(store, {
          operationId: controller.operationId,
          appId: controller.appId,
          continuationKind: 'multi_select',
          retryText: `按用户选择继续：${selection.join(', ')}`,
        });
        const resolved = resolveCardActionToRetry(
          nativeAction(controller.operationId, 'multi_select_confirm', ctx.openId, {
            selected: selection,
          }),
          store,
        );
        if (resolved.duplicate) {
          return { toast: { type: 'warning', content: '决策已提交过（幂等保护）。' } };
        }
        markRetryDelivered(store, controller.operationId);
        return {
          toast: { type: 'success', content: `决策已提交：${selection.join(', ')}` },
          card: controller.buildCardPayload() as Record<string, any>,
        };
      });
      dispatcher.onAction('multi_select_cancel', () => {
        controller.cancel();
        return {
          toast: { type: 'info', content: '已取消选择。' },
          card: controller.buildCardPayload() as Record<string, any>,
        };
      });
      return {
        templateName: name,
        payload: controller.buildCardPayload() as Record<string, any>,
        dispatcher,
        controller,
        store,
      };
    }

    case 'error_retry': {
      const controller = new ErrorRetryCardController({
        appId: 'cli_demo',
        operationId: 'sim-error-0001',
        error: 'delivery timeout: card update did not acknowledge within 5000ms',
        context: '步骤 3/4「测试执行」',
      });
      bindCardContinuation(store, {
        operationId: controller.operationId,
        appId: controller.appId,
        continuationKind: 'error_retry',
        retryText: controller.retryText,
      });
      dispatcher.onAction('error_retry', (ctx) => {
        const resolved = resolveCardActionToRetry(
          nativeAction(controller.operationId, 'error_retry', ctx.openId, ctx.actionValue),
          store,
        );
        if (resolved.duplicate) {
          return {
            toast: { type: 'warning', content: '重试请求已在处理中（幂等保护），重复点击已忽略。' },
          };
        }
        if (options.deliveryFailure) {
          markRetryFailed(store, controller.operationId, { reason: 'simulated delivery timeout' });
          controller.recordFailure('delivery failed: simulated timeout, continuation reset to waiting');
          return {
            toast: { type: 'danger', content: '重试投递失败：continuation 已回滚到 waiting，请再次点击重试。' },
            card: controller.buildCardPayload() as Record<string, any>,
          };
        }
        markRetryDelivered(store, controller.operationId);
        controller.recordRetrySuccess();
        return {
          toast: { type: 'success', content: '重试成功，任务已恢复。' },
          card: controller.buildCardPayload() as Record<string, any>,
        };
      });
      dispatcher.onAction('error_cancel', (ctx) => {
        const resolved = resolveCardActionToRetry(
          nativeAction(controller.operationId, 'error_cancel', ctx.openId, ctx.actionValue),
          store,
        );
        if (resolved.duplicate) {
          return { toast: { type: 'warning', content: '已处理过（幂等保护）。' } };
        }
        markRetryDelivered(store, controller.operationId);
        controller.abandon();
        return {
          toast: { type: 'info', content: '已放弃重试。' },
          card: controller.buildCardPayload() as Record<string, any>,
        };
      });
      return {
        templateName: name,
        payload: controller.buildCardPayload() as Record<string, any>,
        dispatcher,
        controller,
        store,
      };
    }
  }
}

export { buildDemoCardPayload, CARD_TEMPLATE_NAMES, isCardTemplateName };
export type { CardPayload, CardTemplateName };
