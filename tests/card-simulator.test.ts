import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { describe, expect, it } from 'vitest';
import {
  CardSimulator,
  buildActionTriggerEvent,
  collectCardActions,
  createTemplateSimulation,
  envSupportsColor,
  larkMdToText,
  renderCardJson,
  runCardSimulationLoop,
} from '../src/card-simulator.js';
import { InteractiveActionDispatcher } from '../src/interactive-dispatcher.js';

function tmpStorePath(): string {
  return path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'card-sim-')), 'continuations.json');
}

function cleanupStore(storePath: string): void {
  fs.rmSync(path.dirname(storePath), { recursive: true, force: true });
}

function fakeTtyStdin(): NodeJS.ReadStream {
  const stream = new EventEmitter() as unknown as NodeJS.ReadStream & Record<string, any>;
  stream.isTTY = true;
  stream.setRawMode = () => stream;
  stream.resume = () => stream;
  stream.pause = () => {};
  stream.setEncoding = () => stream;
  return stream;
}

describe('card-simulator rendering', () => {
  it('renders header, markdown subset, buttons and notes without ANSI when color is off', () => {
    const payload = {
      config: { wide_screen_mode: true, update_multi: true },
      header: { title: { tag: 'plain_text', content: '计划确认' }, template: 'blue' },
      elements: [
        { tag: 'div', text: { tag: 'lark_md', content: '**加粗** 普通 `code`' } },
        { tag: 'div', text: { tag: 'lark_md', content: '<font color="red">**错误**：坏了</font>' } },
        { tag: 'div', text: { tag: 'lark_md', content: '> 引用一行' } },
        { tag: 'hr' },
        {
          tag: 'action',
          actions: [
            { tag: 'button', text: { tag: 'plain_text', content: '批准' }, type: 'primary', value: { action: 'ok' } },
            { tag: 'button', text: { tag: 'plain_text', content: '驳回' }, type: 'danger', value: { action: 'no' } },
          ],
        },
        { tag: 'note', elements: [{ tag: 'plain_text', content: 'operation_id: x1' }] },
      ],
    };
    const text = renderCardJson(payload, { color: false, width: 40 });
    expect(text).toContain('计划确认');
    expect(text).toContain('加粗');
    expect(text).toContain('code');
    expect(text).toContain('错误：坏了');
    expect(text).toContain('▎引用一行');
    expect(text).toContain('[1] 批准');
    expect(text).toContain('[2] 驳回');
    expect(text).toContain('operation_id: x1');
    expect(text).not.toContain('\x1b[');
    expect(text).not.toContain('<font');
    expect(text).not.toContain('**');
  });

  it('renders ANSI codes when color is on and highlights the selected action', () => {
    const payload = {
      header: { title: { tag: 'plain_text', content: 'T' }, template: 'green' },
      elements: [
        {
          tag: 'action',
          actions: [
            { tag: 'button', text: { tag: 'plain_text', content: 'A' }, value: {} },
            { tag: 'button', text: { tag: 'plain_text', content: 'B' }, value: {} },
          ],
        },
      ],
    };
    const text = renderCardJson(payload, { color: true, selectedAction: 1 });
    expect(text).toContain('\x1b[');
    expect(text).toContain('▶');
  });

  it('larkMdToText strips span tags and keeps plain text', () => {
    expect(larkMdToText('<span data-tag="gray">细节</span>', false)).toBe('细节');
  });

  it('envSupportsColor honors NO_COLOR and FORCE_COLOR', () => {
    const stream = { isTTY: true };
    const prevNoColor = process.env.NO_COLOR;
    const prevForce = process.env.FORCE_COLOR;
    try {
      process.env.NO_COLOR = '1';
      process.env.FORCE_COLOR = '';
      expect(envSupportsColor(stream as any)).toBe(false);
      process.env.NO_COLOR = '';
      process.env.FORCE_COLOR = '1';
      expect(envSupportsColor(stream as any)).toBe(true);
      process.env.NO_COLOR = '';
      process.env.FORCE_COLOR = '';
      expect(envSupportsColor({ isTTY: false } as any)).toBe(false);
    } finally {
      if (prevNoColor === undefined) delete process.env.NO_COLOR;
      else process.env.NO_COLOR = prevNoColor;
      if (prevForce === undefined) delete process.env.FORCE_COLOR;
      else process.env.FORCE_COLOR = prevForce;
    }
  });

  it('collectCardActions flattens buttons across action elements in order', () => {
    const payload = {
      elements: [
        {
          tag: 'action',
          actions: [
            { tag: 'button', text: { tag: 'plain_text', content: 'one' }, value: { action: 'one' } },
          ],
        },
        { tag: 'div', text: 'skip' },
        {
          tag: 'action',
          actions: [
            { tag: 'button', text: { tag: 'plain_text', content: 'two' }, value: { action: 'two' } },
            { tag: 'button', text: { tag: 'plain_text', content: 'three' }, value: { action: 'three' } },
          ],
        },
      ],
    };
    const actions = collectCardActions(payload);
    expect(actions.map((a) => a.label)).toEqual(['one', 'two', 'three']);
    expect(actions.map((a) => a.index)).toEqual([0, 1, 2]);
  });

  it('buildActionTriggerEvent round-trips through InteractiveActionDispatcher.parseContext', () => {
    const event = buildActionTriggerEvent({
      payload: {},
      value: { operation_id: 'op-9', action: 'plan_approve' },
      actorOpenId: 'ou_clicker',
      messageId: 'om_1',
      chatId: 'oc_1',
    });
    const ctx = InteractiveActionDispatcher.parseContext(event);
    expect(ctx.openId).toBe('ou_clicker');
    expect(ctx.messageId).toBe('om_1');
    expect(ctx.chatId).toBe('oc_1');
    expect(ctx.tag).toBe('button');
    expect(ctx.actionValue).toEqual({ operation_id: 'op-9', action: 'plan_approve' });
  });
});

describe('CardSimulator click dispatch', () => {
  it('routes clicks through the dispatcher and applies in-place card updates', async () => {
    const updatedCard = {
      header: { title: { tag: 'plain_text', content: '更新后' }, template: 'green' },
      elements: [],
    };
    const dispatcher = new InteractiveActionDispatcher().onAction('do_it', () => ({
      toast: { type: 'success', content: 'done' },
      card: updatedCard,
    }));
    const lines: string[] = [];
    const simulator = new CardSimulator({
      payload: {
        header: { title: { tag: 'plain_text', content: '初始' }, template: 'blue' },
        elements: [
          {
            tag: 'action',
            actions: [
              { tag: 'button', text: { tag: 'plain_text', content: '执行' }, value: { action: 'do_it' } },
            ],
          },
        ],
      },
      dispatcher,
      color: false,
      output: (line) => lines.push(line),
    });
    const response = await simulator.click(0);
    expect(response.toast?.content).toBe('done');
    expect(simulator.payload.header.title.content).toBe('更新后');
    expect(lines.join('\n')).toContain('toast[success] done');
    await expect(simulator.click(5)).rejects.toThrow(/no clickable action at index 5/);
  });
});

describe('createTemplateSimulation', () => {
  it('rejects unknown template names', () => {
    expect(() => createTemplateSimulation('nope')).toThrow(/unknown template/);
  });

  it('typewriter demo renders a card without buttons', () => {
    const sim = createTemplateSimulation('typewriter', { storePath: null });
    expect(collectCardActions(sim.payload)).toHaveLength(0);
    expect(JSON.stringify(sim.payload)).toContain('sim-typewriter-0001');
  });

  it('plan confirmation: approve delivers the retry, duplicate clicks are suppressed', async () => {
    const storePath = tmpStorePath();
    try {
      const sim = createTemplateSimulation('plan_confirmation', { storePath, freshStore: true });

      const first = new CardSimulator({
        payload: sim.payload,
        dispatcher: sim.dispatcher,
        color: false,
        output: () => {},
        actorOpenId: 'ou_owner',
      });
      const firstResponse = await first.click(
        first.actions().findIndex((a) => a.value.action === 'plan_approve'),
      );
      expect(firstResponse.toast?.type).toBe('success');
      expect(JSON.stringify(first.payload)).toContain('已批准');
      expect(sim.store.load('sim-plan-0001')?.status).toBe('delivered');

      // Replay the same click against the pre-update payload (double click /
      // redelivered callback): suppressed by the idempotency guard.
      const replay = new CardSimulator({
        payload: sim.payload,
        dispatcher: sim.dispatcher,
        color: false,
        output: () => {},
      });
      const second = await replay.click(
        replay.actions().findIndex((a) => a.value.action === 'plan_approve'),
      );
      expect(second.toast?.type).toBe('warning');
      expect(second.card).toBeUndefined();
      expect(sim.store.load('sim-plan-0001')?.status).toBe('delivered');
    } finally {
      cleanupStore(storePath);
    }
  });

  it('clickRepeated races both callbacks on the stale payload and lands one update', async () => {
    const storePath = tmpStorePath();
    try {
      const sim = createTemplateSimulation('plan_confirmation', { storePath, freshStore: true });
      const lines: string[] = [];
      const simulator = new CardSimulator({
        payload: sim.payload,
        dispatcher: sim.dispatcher,
        color: false,
        output: (line) => lines.push(line),
      });
      const approveIndex = simulator.actions().findIndex((a) => a.value.action === 'plan_approve');

      const responses = await simulator.clickRepeated(approveIndex, 2);
      expect(responses.map((r) => r.toast?.type)).toEqual(['success', 'warning']);
      // The delivered card update still lands exactly once.
      expect(JSON.stringify(simulator.payload)).toContain('已批准');
      expect(sim.store.load('sim-plan-0001')?.status).toBe('delivered');
      await expect(simulator.clickRepeated(approveIndex, 2)).rejects.toThrow(/no clickable action/);
    } finally {
      cleanupStore(storePath);
    }
  });

  it('plan confirmation: delivery failure rolls back to waiting, re-click succeeds', async () => {
    const storePath = tmpStorePath();
    try {
      const sim = createTemplateSimulation('plan_confirmation', {
        storePath,
        freshStore: true,
        deliveryFailure: true,
      });
      const simulator = new CardSimulator({
        payload: sim.payload,
        dispatcher: sim.dispatcher,
        color: false,
        output: () => {},
      });
      const approveIndex = () =>
        simulator.actions().findIndex((a) => a.value.action === 'plan_approve');

      const failed = await simulator.click(approveIndex());
      expect(failed.toast?.type).toBe('danger');
      expect(sim.store.load('sim-plan-0001')?.status).toBe('waiting');
      expect(sim.store.load('sim-plan-0001')?.payload.metadata?.delivery_failures).toBe(1);
      // Card still shows the pending plan with buttons — no dirty state.
      expect(simulator.actions().some((a) => a.value.action === 'plan_approve')).toBe(true);

      // Recovery: a healthy simulation on the same store delivers the pending click.
      const healthy = createTemplateSimulation('plan_confirmation', {
        storePath,
        freshStore: false,
      });
      const healthySimulator = new CardSimulator({
        payload: healthy.payload,
        dispatcher: healthy.dispatcher,
        color: false,
        output: () => {},
      });
      const recovered = await healthySimulator.click(
        healthySimulator.actions().findIndex((a) => a.value.action === 'plan_approve'),
      );
      expect(recovered.toast?.type).toBe('success');
      expect(healthy.store.load('sim-plan-0001')?.status).toBe('delivered');
    } finally {
      cleanupStore(storePath);
    }
  });

  it('multi-select: toggles update the card, confirm enforces the minimum', async () => {
    const sim = createTemplateSimulation('multi_select', { storePath: null });
    const simulator = new CardSimulator({
      payload: sim.payload,
      dispatcher: sim.dispatcher,
      color: false,
      output: () => {},
    });

    const toggleIndex = (option: string) =>
      simulator
        .actions()
        .findIndex((a) => a.value.action === 'multi_select_toggle' && a.value.option === option);

    await simulator.click(toggleIndex('runtime'));
    expect((sim.controller as any).selected.has('runtime')).toBe(true);
    await simulator.click(toggleIndex('docs'));
    expect((sim.controller as any).selected.size).toBe(2);

    const confirmIndex = () =>
      simulator.actions().findIndex((a) => a.value.action === 'multi_select_confirm');
    const confirmed = await simulator.click(confirmIndex());
    expect(confirmed.toast?.type).toBe('success');
    expect(JSON.stringify(simulator.payload)).toContain('决策已提交');
    expect(sim.store.load('sim-multiselect-0001')?.status).toBe('delivered');

    // Replay confirm against the pre-confirmation payload.
    const replay = new CardSimulator({
      payload: sim.payload,
      dispatcher: sim.dispatcher,
      color: false,
      output: () => {},
    });
    const again = await replay.click(
      replay.actions().findIndex((a) => a.value.action === 'multi_select_confirm'),
    );
    expect(again.toast?.type).toBe('warning');
  });

  it('error retry: delivery failure records an attempt, next retry recovers', async () => {
    const storePath = tmpStorePath();
    try {
      const failing = createTemplateSimulation('error_retry', {
        storePath,
        freshStore: true,
        deliveryFailure: true,
      });
      const failingSimulator = new CardSimulator({
        payload: failing.payload,
        dispatcher: failing.dispatcher,
        color: false,
        output: () => {},
      });
      const failed = await failingSimulator.click(
        failingSimulator.actions().findIndex((a) => a.value.action === 'error_retry'),
      );
      expect(failed.toast?.type).toBe('danger');
      expect((failing.controller as any).attempts).toBe(2);
      expect(failing.store.load('sim-error-0001')?.status).toBe('waiting');
      // Still retryable — no dirty terminal state after the timeout.
      expect(
        failingSimulator.actions().some((a) => a.value.action === 'error_retry'),
      ).toBe(true);

      const recovered = createTemplateSimulation('error_retry', { storePath, freshStore: false });
      const recoveredSimulator = new CardSimulator({
        payload: recovered.payload,
        dispatcher: recovered.dispatcher,
        color: false,
        output: () => {},
      });
      const success = await recoveredSimulator.click(
        recoveredSimulator.actions().findIndex((a) => a.value.action === 'error_retry'),
      );
      expect(success.toast?.type).toBe('success');
      expect(recovered.store.load('sim-error-0001')?.status).toBe('delivered');
    } finally {
      cleanupStore(storePath);
    }
  });

  it('progress tracking: cancel delivers, replayed cancel is a duplicate', async () => {
    const storePath = tmpStorePath();
    try {
      const sim = createTemplateSimulation('progress_tracking', { storePath, freshStore: true });
      const simulator = new CardSimulator({
        payload: sim.payload,
        dispatcher: sim.dispatcher,
        color: false,
        output: () => {},
      });
      const first = await simulator.click(
        simulator.actions().findIndex((a) => a.value.action === 'progress_cancel'),
      );
      expect(first.toast?.type).toBe('success');
      expect(sim.store.load('sim-progress-0001')?.status).toBe('delivered');

      const replay = new CardSimulator({
        payload: sim.payload,
        dispatcher: sim.dispatcher,
        color: false,
        output: () => {},
      });
      const second = await replay.click(
        replay.actions().findIndex((a) => a.value.action === 'progress_cancel'),
      );
      expect(second.toast?.type).toBe('warning');
    } finally {
      cleanupStore(storePath);
    }
  });
});

describe('runCardSimulationLoop', () => {
  it('falls back to a single render when stdin is not a TTY', async () => {
    const lines: string[] = [];
    const sim = createTemplateSimulation('typewriter', { storePath: null });
    const simulator = new CardSimulator({
      payload: sim.payload,
      color: false,
      output: (line) => lines.push(line),
    });
    const stdin = new EventEmitter() as unknown as NodeJS.ReadStream;
    await runCardSimulationLoop(simulator, { stdin, output: (line) => lines.push(line) });
    expect(lines.join('\n')).toContain('sim-typewriter-0001');
  });

  it('supports selection, click and quit on a fake TTY stdin', async () => {
    const storePath = tmpStorePath();
    try {
      const sim = createTemplateSimulation('plan_confirmation', { storePath, freshStore: true });
      const lines: string[] = [];
      const simulator = new CardSimulator({
        payload: sim.payload,
        dispatcher: sim.dispatcher,
        color: false,
        output: (line) => lines.push(line),
      });
      const stdin = fakeTtyStdin();
      const loop = runCardSimulationLoop(simulator, {
        stdin,
        output: (line) => lines.push(line),
      });
      await new Promise((resolve) => setTimeout(resolve, 10));
      stdin.emit('keypress', null, { name: 'down' }); // select index 1 = reject
      await new Promise((resolve) => setTimeout(resolve, 10));
      stdin.emit('keypress', null, { name: 'return' });
      await new Promise((resolve) => setTimeout(resolve, 20));
      stdin.emit('keypress', null, { name: 'q' });
      await loop;

      expect((sim.controller as any).decision).toBe('rejected'); // index 1 = reject
      expect(sim.store.load('sim-plan-0001')?.status).toBe('delivered');
      expect(lines.join('\n')).toContain('toast[success]');
    } finally {
      cleanupStore(storePath);
    }
  });
});
