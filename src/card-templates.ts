/**
 * Plug-and-play Feishu CardKit 2.0 card templates for agent workflows.
 *
 * Five templates ship here: agent plan confirmation, streaming typewriter,
 * progress tracking, multi-select decision, and error retry. Each template is a
 * pure payload builder plus a small state controller that rebuilds the payload
 * as the workflow advances. Every button value carries `operation_id` and
 * `action`, so callbacks bind to a native continuation through
 * `resolveCardActionToRetry` without extra glue.
 */

import { NativeContinuationRecord } from './native-contract.js';
import { ContinuationState, FileContinuationStore, newOperationId } from './runtime-cards.js';
import {
  StreamingCardController,
  StreamingCardOptions,
  StreamingCardState,
} from './streaming-card.js';

export type CardTemplateColor = 'blue' | 'green' | 'red' | 'grey' | 'yellow' | 'turquoise';

export interface CardButton {
  tag: 'button';
  text: { tag: 'plain_text'; content: string };
  type?: 'primary' | 'default' | 'danger';
  value: Record<string, any>;
}

export interface CardPayload {
  config: { wide_screen_mode: boolean; update_multi: boolean };
  header: { title: { tag: 'plain_text'; content: string }; template: CardTemplateColor };
  elements: Array<Record<string, any>>;
  [key: string]: any;
}

export function cardPayload(options: {
  title: string;
  color: CardTemplateColor;
  elements: Array<Record<string, any>>;
}): CardPayload {
  return {
    config: { wide_screen_mode: true, update_multi: true },
    header: { title: { tag: 'plain_text', content: options.title }, template: options.color },
    elements: options.elements,
  };
}

export function mdDiv(content: string): Record<string, any> {
  return { tag: 'div', text: { tag: 'lark_md', content } };
}

export function noteElement(text: string): Record<string, any> {
  return { tag: 'note', elements: [{ tag: 'plain_text', content: text }] };
}

export function hrElement(): Record<string, any> {
  return { tag: 'hr' };
}

export function cardButton(
  label: string,
  action: string,
  options?: {
    buttonType?: 'primary' | 'default' | 'danger';
    operationId?: string;
    value?: Record<string, any>;
  },
): CardButton {
  return {
    tag: 'button',
    text: { tag: 'plain_text', content: label },
    type: options?.buttonType ?? 'default',
    value: {
      operation_id: options?.operationId ?? '',
      action,
      ...(options?.value ?? {}),
    },
  };
}

export function actionElement(buttons: CardButton[]): Record<string, any> {
  return { tag: 'action', actions: buttons };
}

/** Persist the native continuation a card's buttons resolve against. */
export function bindCardContinuation(
  store: FileContinuationStore,
  options: {
    operationId?: string;
    appId: string;
    continuationKind: string;
    retryText: string;
    userOpenId?: string | null;
    metadata?: Record<string, any>;
  },
): NativeContinuationRecord {
  const record = new NativeContinuationRecord({
    operationId: options.operationId ?? newOperationId(),
    appId: options.appId,
    continuationKind: options.continuationKind,
    retryText: options.retryText,
    userOpenId: options.userOpenId ?? null,
    status: 'waiting',
    metadata: options.metadata,
  });
  const state: ContinuationState = record.toState();
  store.save(state);
  return record;
}

/* ------------------------------------------------------------------ */
/* 1. Agent plan confirmation card                                     */
/* ------------------------------------------------------------------ */

export interface PlanStep {
  title: string;
  detail?: string | null;
}

export type PlanDecision = 'pending' | 'approved' | 'rejected';

export class AgentPlanCardController {
  readonly operationId: string;
  readonly appId: string;
  readonly steps: PlanStep[];
  decision: PlanDecision = 'pending';
  decidedBy: string | null = null;
  readonly createdAt: number = Date.now();

  constructor(options: {
    appId: string;
    operationId?: string;
    steps: Array<string | PlanStep>;
    title?: string;
  }) {
    this.appId = options.appId;
    this.operationId = options.operationId ?? newOperationId();
    this.steps = options.steps.map((step) =>
      typeof step === 'string' ? { title: step } : { ...step },
    );
    this.title = options.title ?? 'Agent 执行计划确认';
  }

  readonly title: string;

  approve(actorOpenId?: string | null): void {
    if (this.decision !== 'pending') return;
    this.decision = 'approved';
    this.decidedBy = actorOpenId ?? null;
  }

  reject(actorOpenId?: string | null): void {
    if (this.decision !== 'pending') return;
    this.decision = 'rejected';
    this.decidedBy = actorOpenId ?? null;
  }

  buildRetryText(): string {
    const list = this.steps.map((step, i) => `${i + 1}. ${step.title}`).join('；');
    return `已确认执行计划：${list}`;
  }

  buildCardPayload(): CardPayload {
    const elements: Array<Record<string, any>> = [];

    if (this.decision === 'pending') {
      elements.push(mdDiv('**Agent 提出了以下执行计划，请确认：**'), { tag: 'hr' });
      this.steps.forEach((step, i) => {
        const detail = step.detail ? `\n<span data-tag="gray">${step.detail}</span>` : '';
        elements.push(mdDiv(`**${i + 1}.** ${step.title}${detail}`));
      });
      elements.push(
        { tag: 'hr' },
        actionElement([
          cardButton('✅ 批准执行', 'plan_approve', {
            buttonType: 'primary',
            operationId: this.operationId,
          }),
          cardButton('❌ 驳回', 'plan_reject', {
            buttonType: 'danger',
            operationId: this.operationId,
          }),
        ]),
        noteElement(`operation_id: ${this.operationId} · 提交于 ${new Date(this.createdAt).toLocaleString()}`),
      );
      return cardPayload({ title: this.title, color: 'blue', elements });
    }

    const verdict =
      this.decision === 'approved'
        ? `✅ **已批准**${this.decidedBy ? `（by \`${this.decidedBy}\`）` : ''}，Agent 继续执行。`
        : `❌ **已驳回**${this.decidedBy ? `（by \`${this.decidedBy}\`）` : ''}，流程终止。`;
    elements.push(mdDiv(verdict), { tag: 'hr' });
    this.steps.forEach((step, i) => {
      elements.push(mdDiv(`**${i + 1}.** ${step.title}`));
    });
    elements.push(noteElement(`operation_id: ${this.operationId}`));
    return cardPayload({
      title: this.decision === 'approved' ? `${this.title} · 已批准` : `${this.title} · 已驳回`,
      color: this.decision === 'approved' ? 'green' : 'red',
      elements,
    });
  }
}

/* ------------------------------------------------------------------ */
/* 2. Streaming typewriter card                                        */
/* ------------------------------------------------------------------ */

export type TypewriterCardState = StreamingCardState;

/**
 * StreamingCardController dressed with template defaults (Chinese lifecycle
 * titles, operation-id footer). Delegates all streaming behavior.
 */
export class TypewriterCardTemplate {
  readonly operationId: string;
  private readonly inner: StreamingCardController;

  constructor(options: StreamingCardOptions & { operationId?: string } = {}) {
    this.operationId = options.operationId ?? newOperationId();
    this.inner = new StreamingCardController({
      title: options.title ?? 'Agent 回复中…',
      initialText: options.initialText,
      throttleMs: options.throttleMs,
      throttleConstants: options.throttleConstants,
      onFlush: options.onFlush,
    });
  }

  get state(): TypewriterCardState {
    return this.inner.getState();
  }

  getContent(): string {
    return this.inner.getContent();
  }

  getReasoningContent(): string {
    return this.inner.getReasoningContent();
  }

  appendToken(token: string): Promise<void> {
    return this.inner.appendToken(token);
  }

  appendReasoningToken(token: string): Promise<void> {
    return this.inner.appendReasoningToken(token);
  }

  updateContent(text: string): Promise<void> {
    return this.inner.updateContent(text);
  }

  complete(finalText?: string): Promise<void> {
    return this.inner.complete(finalText);
  }

  abort(reason?: string): Promise<void> {
    return this.inner.abort(reason);
  }

  error(err: Error | string): Promise<void> {
    return this.inner.error(err);
  }

  buildCardPayload(): CardPayload {
    const payload = this.inner.buildCardPayload() as CardPayload;
    payload.elements.push(
      noteElement(`streaming · operation_id: ${this.operationId} · state: ${this.inner.getState()}`),
    );
    return payload;
  }
}

/* ------------------------------------------------------------------ */
/* 3. Progress tracking card                                           */
/* ------------------------------------------------------------------ */

export type ProgressStepStatus = 'pending' | 'running' | 'done' | 'failed';

export interface ProgressStep {
  id: string;
  title: string;
  status: ProgressStepStatus;
  detail: string | null;
}

const PROGRESS_ICONS: Record<ProgressStepStatus, string> = {
  pending: '⚪',
  running: '⏳',
  done: '✅',
  failed: '❌',
};

export function renderProgressBar(percent: number, width = 10): string {
  const clamped = Math.max(0, Math.min(100, Math.round(percent)));
  const filled = Math.round((clamped / 100) * width);
  return `${'▓'.repeat(filled)}${'░'.repeat(width - filled)} ${clamped}%`;
}

export class ProgressCardController {
  readonly operationId: string;
  readonly appId: string;
  readonly steps: ProgressStep[];
  summary: string | null = null;
  cancelled = false;
  private readonly title: string;

  constructor(options: {
    appId: string;
    operationId?: string;
    title?: string;
    steps: Array<string | { id?: string; title: string }>;
  }) {
    this.appId = options.appId;
    this.operationId = options.operationId ?? newOperationId();
    this.title = options.title ?? '任务执行进度';
    this.steps = options.steps.map((step, i) => ({
      id: typeof step === 'string' ? `step-${i + 1}` : step.id ?? `step-${i + 1}`,
      title: typeof step === 'string' ? step : step.title,
      status: 'pending' as ProgressStepStatus,
      detail: null,
    }));
  }

  private firstPending(): ProgressStep | undefined {
    return this.steps.find((step) => step.status === 'pending');
  }

  start(stepId?: string): ProgressStep | null {
    if (this.cancelled) return null;
    const step = stepId
      ? this.steps.find((s) => s.id === stepId)
      : (this.steps.find((s) => s.status === 'running') ?? this.firstPending());
    if (!step || step.status === 'done' || step.status === 'failed') return null;
    step.status = 'running';
    return step;
  }

  completeStep(stepId: string, detail?: string | null): void {
    const step = this.steps.find((s) => s.id === stepId);
    if (step && step.status !== 'failed') {
      step.status = 'done';
      step.detail = detail ?? step.detail;
    }
  }

  failStep(stepId: string, detail?: string | null): void {
    const step = this.steps.find((s) => s.id === stepId);
    if (step) {
      step.status = 'failed';
      step.detail = detail ?? step.detail;
    }
  }

  cancel(actorOpenId?: string | null): void {
    if (this.summary) return;
    this.cancelled = true;
    this.summary = actorOpenId ? `已被 ${actorOpenId} 取消` : '已被用户取消';
  }

  finish(summary: string): void {
    this.summary = summary;
  }

  percent(): number {
    const done = this.steps.filter((s) => s.status === 'done').length;
    return this.steps.length === 0 ? 0 : Math.round((done / this.steps.length) * 100);
  }

  buildCardPayload(): CardPayload {
    const elements: Array<Record<string, any>> = [];
    const failed = this.steps.some((s) => s.status === 'failed');

    elements.push(
      mdDiv(
        `**总进度** \`${renderProgressBar(this.percent())}\` · ${this.steps.filter((s) => s.status === 'done').length}/${this.steps.length} 步`,
      ),
      { tag: 'hr' },
    );

    for (const step of this.steps) {
      const icon = PROGRESS_ICONS[step.status];
      const detail = step.detail ? `\n<span data-tag="gray">${step.detail}</span>` : '';
      elements.push(mdDiv(`${icon} **${step.title}**${detail}`));
    }

    if (this.summary) {
      elements.push({ tag: 'hr' }, mdDiv(`**结果**：${this.summary}`));
    } else if (!this.cancelled) {
      elements.push(
        { tag: 'hr' },
        actionElement([
          cardButton('取消任务', 'progress_cancel', {
            buttonType: 'danger',
            operationId: this.operationId,
          }),
        ]),
      );
    }

    const color: CardTemplateColor = this.cancelled
      ? 'grey'
      : failed
        ? 'red'
        : this.summary
          ? 'green'
          : 'blue';
    elements.push(noteElement(`progress · operation_id: ${this.operationId}`));
    return cardPayload({ title: this.title, color, elements });
  }
}

/* ------------------------------------------------------------------ */
/* 4. Multi-select decision card                                       */
/* ------------------------------------------------------------------ */

export interface MultiSelectOption {
  value: string;
  label: string;
}

export class MultiSelectCardController {
  readonly operationId: string;
  readonly appId: string;
  readonly options: MultiSelectOption[];
  readonly selected: Set<string> = new Set();
  confirmed = false;
  cancelled = false;
  confirmedBy: string | null = null;
  private readonly title: string;
  private readonly question: string;
  private readonly minSelect: number;
  private readonly maxSelect: number | null;

  constructor(options: {
    appId: string;
    operationId?: string;
    title?: string;
    question: string;
    options: Array<string | MultiSelectOption>;
    minSelect?: number;
    maxSelect?: number | null;
  }) {
    this.appId = options.appId;
    this.operationId = options.operationId ?? newOperationId();
    this.title = options.title ?? '多选决策';
    this.question = options.question;
    this.options = options.options.map((option) =>
      typeof option === 'string' ? { value: option, label: option } : { ...option },
    );
    this.minSelect = options.minSelect ?? 1;
    this.maxSelect = options.maxSelect ?? null;
  }

  toggle(value: string): boolean {
    if (this.confirmed || this.cancelled) return false;
    if (this.selected.has(value)) {
      this.selected.delete(value);
      return false;
    }
    if (this.maxSelect !== null && this.selected.size >= this.maxSelect) {
      return false;
    }
    this.selected.add(value);
    return true;
  }

  confirm(actorOpenId?: string | null): string[] | null {
    if (this.confirmed || this.cancelled) return null;
    if (this.selected.size < this.minSelect) return null;
    this.confirmed = true;
    this.confirmedBy = actorOpenId ?? null;
    return [...this.selected];
  }

  cancel(): void {
    if (this.confirmed) return;
    this.cancelled = true;
  }

  buildCardPayload(): CardPayload {
    const elements: Array<Record<string, any>> = [mdDiv(`**${this.question}**`), { tag: 'hr' }];

    if (this.confirmed) {
      const chosen = this.options
        .filter((option) => this.selected.has(option.value))
        .map((option) => `- ✅ ${option.label}`)
        .join('\n');
      elements.push(
        mdDiv(chosen || '- （空）'),
        { tag: 'hr' },
        mdDiv(
          `✅ **决策已提交**${this.confirmedBy ? `（by \`${this.confirmedBy}\`）` : ''}，选中 ${this.selected.size} 项。`,
        ),
      );
    } else if (this.cancelled) {
      elements.push(mdDiv('🚫 **已取消，未做选择。**'));
    } else {
      // Options as toggle buttons: each click emits `multi_select_toggle` with the
      // option value, mirroring how a real Feishu card flips selection in place.
      for (let i = 0; i < this.options.length; i += 2) {
        const row = this.options.slice(i, i + 2).map((option) =>
          cardButton(
            `${this.selected.has(option.value) ? '☑' : '☐'} ${option.label}`,
            'multi_select_toggle',
            {
              buttonType: this.selected.has(option.value) ? 'primary' : 'default',
              operationId: this.operationId,
              value: { option: option.value },
            },
          ),
        );
        elements.push(actionElement(row));
      }
      elements.push(
        { tag: 'hr' },
        actionElement([
          cardButton('确认选择', 'multi_select_confirm', {
            buttonType: 'primary',
            operationId: this.operationId,
            value: { selected: [...this.selected] },
          }),
          cardButton('取消', 'multi_select_cancel', {
            operationId: this.operationId,
          }),
        ]),
        noteElement(
          `至少选 ${this.minSelect} 项${this.maxSelect !== null ? `，至多选 ${this.maxSelect} 项` : ''} · 当前已选 ${this.selected.size} 项`,
        ),
      );
    }

    const color: CardTemplateColor = this.confirmed
      ? 'green'
      : this.cancelled
        ? 'grey'
        : 'blue';
    return cardPayload({ title: this.title, color, elements });
  }
}

/* ------------------------------------------------------------------ */
/* 5. Error retry card                                                 */
/* ------------------------------------------------------------------ */

export interface RetryAttempt {
  at: number;
  error: string;
}

export class ErrorRetryCardController {
  readonly operationId: string;
  readonly appId: string;
  attempts: number = 0;
  resolved = false;
  abandoned = false;
  readonly history: RetryAttempt[] = [];
  private latestError: string;
  private context: string | null;
  private summary: string | null = null;
  private readonly title: string;

  constructor(options: {
    appId: string;
    operationId?: string;
    title?: string;
    error: string;
    context?: string | null;
  }) {
    this.appId = options.appId;
    this.operationId = options.operationId ?? newOperationId();
    this.title = options.title ?? '执行失败 · 可重试';
    this.latestError = options.error;
    this.context = options.context ?? null;
    this.recordFailure(options.error);
  }

  get retryText(): string {
    return `重试失败的任务（第 ${this.attempts + 1} 次）：${this.context ?? this.latestError}`;
  }

  get contextText(): string | null {
    return this.context;
  }

  recordFailure(error: string): void {
    this.latestError = error;
    this.attempts += 1;
    this.history.push({ at: Date.now(), error });
  }

  recordRetrySuccess(summary: string = '重试成功，任务已完成。'): void {
    this.resolved = true;
    this.summary = summary;
  }

  abandon(): void {
    if (this.resolved) return;
    this.abandoned = true;
  }

  buildCardPayload(): CardPayload {
    const elements: Array<Record<string, any>> = [];

    if (this.resolved) {
      elements.push(mdDiv(`✅ **${this.summary}**`));
    } else if (this.abandoned) {
      elements.push(
        mdDiv('🚫 **已放弃重试。**'),
        mdDiv(`<span data-tag="gray">最后错误：${this.latestError}</span>`),
      );
    } else {
      elements.push(
        mdDiv(`<font color="red">**错误**：</font>${this.latestError}`),
      );
      if (this.context) {
        elements.push(mdDiv(`<span data-tag="gray">上下文：${this.context}</span>`));
      }
      if (this.history.length > 1) {
        elements.push(
          { tag: 'hr' },
          mdDiv(`**已尝试 ${this.history.length} 次：**`),
          ...this.history
            .slice(-3)
            .map((attempt, i) =>
              mdDiv(
                `${i + 1}. \`${new Date(attempt.at).toLocaleTimeString()}\` — ${attempt.error}`,
              ),
            ),
        );
      }
      elements.push(
        { tag: 'hr' },
        actionElement([
          cardButton('🔄 重试', 'error_retry', {
            buttonType: 'primary',
            operationId: this.operationId,
            value: { attempt: this.attempts },
          }),
          cardButton('放弃', 'error_cancel', {
            buttonType: 'danger',
            operationId: this.operationId,
          }),
        ]),
      );
    }

    elements.push(noteElement(`retry · operation_id: ${this.operationId} · attempts: ${this.attempts}`));
    return cardPayload({
      title: this.resolved ? '任务已恢复' : this.title,
      color: this.resolved ? 'green' : 'red',
      elements,
    });
  }
}

/* ------------------------------------------------------------------ */
/* Registry for the simulator / CLI                                    */
/* ------------------------------------------------------------------ */

export const CARD_TEMPLATE_NAMES = [
  'plan_confirmation',
  'typewriter',
  'progress_tracking',
  'multi_select',
  'error_retry',
] as const;

export type CardTemplateName = (typeof CARD_TEMPLATE_NAMES)[number];

export function isCardTemplateName(value: string): value is CardTemplateName {
  return (CARD_TEMPLATE_NAMES as readonly string[]).includes(value);
}

/**
 * Demo payload for each template, used by `card-templates` dump and the
 * interactive simulator so they run without any external fixture.
 */
export function buildDemoCardPayload(name: CardTemplateName): CardPayload {
  switch (name) {
    case 'plan_confirmation':
      return new AgentPlanCardController({
        appId: 'cli_demo',
        operationId: 'demo-plan-0001',
        steps: [
          { title: '拉取仓库最新代码', detail: 'git pull --rebase origin main' },
          { title: '运行单测与类型检查', detail: 'pnpm test && pnpm typecheck' },
          { title: '构建并输出产物摘要' },
        ],
      }).buildCardPayload();
    case 'typewriter': {
      const controller = new TypewriterCardTemplate({
        operationId: 'demo-typewriter-0001',
        initialText: '正在阅读 `src/card-templates.ts`…\n\n已定位 3 个可优化的分发分支，正在生成重构建议。',
      });
      return controller.buildCardPayload();
    }
    case 'progress_tracking': {
      const controller = new ProgressCardController({
        appId: 'cli_demo',
        operationId: 'demo-progress-0001',
        steps: ['环境检查', '依赖安装', '测试执行', '构建产物'],
      });
      controller.start();
      controller.completeStep('step-1', 'node 26 / pnpm 10 就绪');
      controller.start();
      return controller.buildCardPayload();
    }
    case 'multi_select':
      return new MultiSelectCardController({
        appId: 'cli_demo',
        operationId: 'demo-multiselect-0001',
        question: '本次发布要包含哪些变更？',
        options: [
          { value: 'runtime', label: '运行时内核收敛' },
          { value: 'cards', label: '卡片模板库' },
          { value: 'docs', label: '文档与决策记录' },
          { value: 'ci', label: 'CI 升级' },
        ],
        minSelect: 1,
      }).buildCardPayload();
    case 'error_retry':
      return new ErrorRetryCardController({
        appId: 'cli_demo',
        operationId: 'demo-error-0001',
        error: 'delivery timeout: card update did not acknowledge within 5000ms',
        context: '步骤 3/4「测试执行」',
      }).buildCardPayload();
  }
}
