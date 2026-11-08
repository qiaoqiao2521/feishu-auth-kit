import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  AgentPlanCardController,
  CARD_TEMPLATE_NAMES,
  ErrorRetryCardController,
  MultiSelectCardController,
  ProgressCardController,
  TypewriterCardTemplate,
  bindCardContinuation,
  buildDemoCardPayload,
  isCardTemplateName,
  renderProgressBar,
} from '../src/card-templates.js';
import { loadNativeContinuation } from '../src/native-contract.js';
import { FileContinuationStore } from '../src/runtime-cards.js';

function buttonsOf(payload: Record<string, any>): Array<Record<string, any>> {
  return payload.elements
    .filter((el: any) => el.tag === 'action')
    .flatMap((el: any) => el.actions);
}

describe('card-templates', () => {
  it('plan card: pending state carries bound buttons, approve locks the decision', () => {
    const controller = new AgentPlanCardController({
      appId: 'cli_x',
      operationId: 'op-plan-1',
      steps: ['第一步', { title: '第二步', detail: '细节' }],
    });
    const pending = controller.buildCardPayload();
    expect(pending.header.template).toBe('blue');
    const buttons = buttonsOf(pending);
    expect(buttons).toHaveLength(2);
    expect(buttons[0].value).toMatchObject({ operation_id: 'op-plan-1', action: 'plan_approve' });
    expect(buttons[1].value).toMatchObject({ operation_id: 'op-plan-1', action: 'plan_reject' });
    expect(JSON.stringify(pending)).toContain('第二步');

    controller.approve('ou_actor');
    const approved = controller.buildCardPayload();
    expect(approved.header.template).toBe('green');
    expect(approved.header.title.content).toContain('已批准');
    expect(buttonsOf(approved)).toHaveLength(0);
    expect(controller.buildRetryText()).toContain('第一步');

    // Decision locked: approve/reject after approval are no-ops.
    controller.reject('ou_other');
    expect(controller.decision).toBe('approved');
  });

  it('plan card: reject renders red terminal state', () => {
    const controller = new AgentPlanCardController({
      appId: 'cli_x',
      operationId: 'op-plan-2',
      steps: ['a'],
    });
    controller.reject();
    const payload = controller.buildCardPayload();
    expect(payload.header.template).toBe('red');
    expect(payload.header.title.content).toContain('已驳回');
  });

  it('typewriter card delegates streaming and appends state note', async () => {
    const controller = new TypewriterCardTemplate({ operationId: 'op-type-1' });
    await controller.appendToken('你好');
    expect(controller.state).toBe('streaming');
    const streaming = controller.buildCardPayload();
    expect(JSON.stringify(streaming)).toContain('▊');
    expect(JSON.stringify(streaming)).toContain('op-type-1');

    await controller.complete('你好，世界');
    expect(controller.state).toBe('completed');
    const done = controller.buildCardPayload();
    expect(JSON.stringify(done)).toContain('你好，世界');
    expect(JSON.stringify(done)).not.toContain('▊');

    await controller.appendToken('ignored');
    expect(controller.getContent()).toBe('你好，世界');
  });

  it('renderProgressBar clamps and fills', () => {
    expect(renderProgressBar(0)).toBe('░░░░░░░░░░ 0%');
    expect(renderProgressBar(50)).toBe('▓▓▓▓▓░░░░░ 50%');
    expect(renderProgressBar(100)).toBe('▓▓▓▓▓▓▓▓▓▓ 100%');
    expect(renderProgressBar(140)).toBe('▓▓▓▓▓▓▓▓▓▓ 100%');
    expect(renderProgressBar(-5)).toBe('░░░░░░░░░░ 0%');
  });

  it('progress card: steps advance, cancel closes the card', () => {
    const controller = new ProgressCardController({
      appId: 'cli_x',
      operationId: 'op-prog-1',
      steps: ['a', 'b', 'c', 'd'],
    });
    expect(controller.start()!.id).toBe('step-1');
    controller.completeStep('step-1', 'ok');
    expect(controller.start()!.id).toBe('step-2');
    expect(controller.percent()).toBe(25);

    const running = controller.buildCardPayload();
    expect(JSON.stringify(running)).toContain('▓');
    expect(JSON.stringify(running)).toContain('⏳');
    expect(buttonsOf(running).some((b) => b.value.action === 'progress_cancel')).toBe(true);

    controller.cancel('ou_actor');
    const cancelled = controller.buildCardPayload();
    expect(cancelled.header.template).toBe('grey');
    expect(buttonsOf(cancelled)).toHaveLength(0);
    expect(controller.start()).toBeNull();
  });

  it('progress card: failure marks the run red, finish marks it green', () => {
    const controller = new ProgressCardController({
      appId: 'cli_x',
      steps: ['a', 'b'],
    });
    controller.start();
    controller.failStep('step-1', 'boom');
    expect(controller.buildCardPayload().header.template).toBe('red');

    const done = new ProgressCardController({ appId: 'cli_x', steps: ['a'] });
    done.start();
    done.completeStep('step-1');
    done.finish('全部完成');
    expect(done.buildCardPayload().header.template).toBe('green');
  });

  it('multi-select card: toggle, max clamp, min guard, confirm and cancel', () => {
    const controller = new MultiSelectCardController({
      appId: 'cli_x',
      operationId: 'op-ms-1',
      question: '选?',
      options: ['a', 'b', 'c'],
      minSelect: 1,
      maxSelect: 2,
    });
    // Below minSelect, confirm is refused.
    expect(controller.confirm()).toBeNull();

    expect(controller.toggle('a')).toBe(true);
    expect(controller.toggle('b')).toBe(true);
    // maxSelect=2: third distinct option refused until one is un-toggled.
    expect(controller.toggle('c')).toBe(false);
    expect(controller.toggle('a')).toBe(false);
    expect(controller.toggle('c')).toBe(true);
    expect(controller.selected.size).toBe(2);

    const pending = controller.buildCardPayload();
    const toggles = buttonsOf(pending).filter((b) => b.value.action === 'multi_select_toggle');
    expect(toggles).toHaveLength(3);
    const confirmButton = buttonsOf(pending).find((b) => b.value.action === 'multi_select_confirm');
    expect(confirmButton?.value.selected).toEqual(['b', 'c']);

    expect(controller.confirm('ou_actor')).toEqual(['b', 'c']);
    const confirmed = controller.buildCardPayload();
    expect(confirmed.header.template).toBe('green');
    expect(buttonsOf(confirmed)).toHaveLength(0);
    expect(controller.toggle('a')).toBe(false);
  });

  it('multi-select card: cancel path renders grey without selection', () => {
    const controller = new MultiSelectCardController({
      appId: 'cli_x',
      question: '选?',
      options: ['a'],
    });
    controller.cancel();
    expect(controller.buildCardPayload().header.template).toBe('grey');
    expect(controller.confirm()).toBeNull();
  });

  it('error retry card: failure history, retry success, abandon', () => {
    const controller = new ErrorRetryCardController({
      appId: 'cli_x',
      operationId: 'op-err-1',
      error: 'first failure',
      context: 'ctx',
    });
    expect(controller.attempts).toBe(1);
    controller.recordFailure('second failure');
    expect(controller.retryText).toContain('第 3 次');

    const failed = controller.buildCardPayload();
    expect(failed.header.template).toBe('red');
    const retryButton = buttonsOf(failed).find((b) => b.value.action === 'error_retry');
    expect(retryButton?.value.attempt).toBe(2);
    expect(JSON.stringify(failed)).toContain('已尝试 2 次');

    controller.recordRetrySuccess();
    const resolved = controller.buildCardPayload();
    expect(resolved.header.template).toBe('green');
  });

  it('error retry card: abandon renders terminal state', () => {
    const controller = new ErrorRetryCardController({
      appId: 'cli_x',
      error: 'boom',
    });
    controller.abandon();
    const payload = controller.buildCardPayload();
    expect(JSON.stringify(payload)).toContain('已放弃重试');
    expect(buttonsOf(payload)).toHaveLength(0);
  });

  it('bindCardContinuation persists a native continuation', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'templates-bind-'));
    try {
      const store = new FileContinuationStore(path.join(tmpDir, 'c.json'));
      const record = bindCardContinuation(store, {
        appId: 'cli_x',
        continuationKind: 'plan_confirmation',
        retryText: '继续',
        metadata: { plan: ['a'] },
      });
      const loaded = loadNativeContinuation(store, record.operationId);
      expect(loaded?.retryText).toBe('继续');
      expect(loaded?.continuationKind).toBe('plan_confirmation');
      expect(loaded?.status).toBe('waiting');
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  it('demo registry covers all five templates and validates names', () => {
    expect(CARD_TEMPLATE_NAMES).toHaveLength(5);
    for (const name of CARD_TEMPLATE_NAMES) {
      expect(isCardTemplateName(name)).toBe(true);
      const payload = buildDemoCardPayload(name);
      expect(payload.header?.title?.content).toBeTruthy();
      expect(Array.isArray(payload.elements)).toBe(true);
    }
    expect(isCardTemplateName('nope')).toBe(false);
  });
});
