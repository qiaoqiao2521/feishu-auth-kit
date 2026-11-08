import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { AgentTurnRequest } from '../src/agent-runtime.js';
import {
  AmmoRoute,
  WaterfallCliRunner,
  answerAutoloopAsk,
  buildFleetBoardCard,
  parseFeishuAskBlock,
  suspendAutoloopAsk,
} from '../src/autoloop.js';
import { FileContinuationStore } from '../src/runtime-cards.js';

describe('autoloop: zero-token suspend, ammo waterfall, and fleet board', () => {
  it('parses trailing feishu-ask and agent-autoloop blocks cleanly', () => {
    const raw = [
      '已检查 T250 安装包，官方 CDN 返回 403。',
      '',
      '```feishu-ask',
      '{"title":"T250 源失效，如何处理？","text":"官方 CDN 403","options":["切换 Winget 镜像","挂起跳过 T250"]}',
      '```',
    ].join('\n');

    const res = parseFeishuAskBlock(raw);
    expect(res.summaryText).toBe('已检查 T250 安装包，官方 CDN 返回 403。');
    expect(res.ask).not.toBeNull();
    expect(res.ask?.title).toBe('T250 源失效，如何处理？');
    expect(res.ask?.options).toEqual(['切换 Winget 镜像', '挂起跳过 T250']);
  });

  it('automatically falls back across ammo routes when first route hits 429 or fails', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'autoloop-test-'));
    const cooldownPath = path.join(tmpDir, 'cooldowns.json');

    const routes: AmmoRoute[] = [
      { id: 'glm-flash', label: 'GLM-5.3-Flash', command: ['mock-glm', '{prompt}'] },
      { id: 'minimax-3.1', label: 'MiniMax-M3.1', command: ['mock-mm', '{prompt}'] },
    ];

    const calls: string[] = [];
    const mockSpawn: any = (bin: string) => {
      calls.push(bin);
      if (bin === 'mock-glm') {
        return { status: 1, stdout: '', stderr: 'HTTP 429 rate_limit_exceeded' };
      }
      return { status: 0, stdout: 'Fixed via MiniMax-M3.1!', stderr: '' };
    };

    const runner = new WaterfallCliRunner({
      routes,
      cooldownPath,
      spawnSyncFn: mockSpawn,
    });

    const dummyCtx: any = {
      eventType: 'test',
      messageId: 'm1',
      chatId: 'c1',
      senderOpenId: 'u1',
      promptText: () => 'Fix T250',
      toDict: () => ({}),
    };
    const result = runner.run(new AgentTurnRequest({ context: dummyCtx, prompt: 'Fix T250' }));

    expect(result.status).toBe('completed');
    expect(result.metadata.selected_route).toBe('minimax-3.1');
    expect(calls).toEqual(['mock-glm', 'mock-mm']);

    // Next run should skip mock-glm because it is in cooldown
    calls.length = 0;
    const result2 = runner.run(new AgentTurnRequest({ context: dummyCtx, prompt: 'Fix T253' }));
    expect(result2.status).toBe('completed');
    expect(calls).toEqual(['mock-mm']);

    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('suspends on feishu-ask with 0 process/token and resumes idempotently on answer', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'autoloop-store-'));
    const store = new FileContinuationStore(path.join(tmpDir, 'continuations.json'));
    const cooldownPath = path.join(tmpDir, 'cooldowns.json');

    const suspended = suspendAutoloopAsk(store, {
      appId: 'cli_test',
      operationId: 'op_t250',
      ask: {
        title: 'T250 怎么处理？',
        options: ['换源重抓', '跳过车道'],
      },
      summaryText: '已定位 CDN 403',
      originalPrompt: '修复 T250 车道',
      botName: '避风港',
      nodeName: '2025',
    });

    expect(suspended.operationId).toBe('op_t250');
    expect(suspended.card.header.title.content).toContain('[避风港] T250 怎么处理？');

    let receivedPrompt = '';
    const runner = new WaterfallCliRunner({
      routes: [{ id: 'mm', label: 'MiniMax-M3.1', command: ['mock-mm', '{prompt}'] }],
      cooldownPath,
      spawnSyncFn: ((_bin: string, args: string[]) => {
        receivedPrompt = args[0];
        return { status: 0, stdout: '已按拍板换源并合入 master', stderr: '' };
      }) as any,
    });

    const ans1 = answerAutoloopAsk(store, runner, {
      operationId: 'op_t250',
      choice: 1,
      actorOpenId: 'ou_owner',
    });
    expect(ans1.duplicate).toBe(false);
    expect(ans1.chosenOption).toBe('换源重抓');
    expect(receivedPrompt).toContain('换源重抓');
    expect(ans1.turnResult?.status).toBe('completed');

    // Second click on same card is blocked by idempotency guard
    const ans2 = answerAutoloopAsk(store, runner, {
      operationId: 'op_t250',
      choice: 1,
      actorOpenId: 'ou_owner',
    });
    expect(ans2.duplicate).toBe(true);
    expect(ans2.turnResult).toBeNull();

    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('renders pure-code fleet board card without invoking any model', () => {
    const card = buildFleetBoardCard({
      updatedAt: '2026-11-08 12:08:00 CST',
      headCommit: 'b9afd0c',
      pendingLanes: 2,
      recentMerged: ['T249 (malwarebytes)', 'T252 (vlc-client)'],
      nodes: [
        {
          node: '2025',
          botName: '避风港',
          role: '总控巡检',
          online: true,
          diskPct: '63%',
          loadAvg: '0.25',
          defaultAmmo: 'opencode (GLM-5.3 / MiniMax-3.1)',
          activeLanes: [],
          tmuxSessions: ['re-overseer'],
        },
        {
          node: 'cy',
          botName: '云栈',
          role: '逆向主厂',
          online: true,
          diskPct: '76%',
          loadAvg: '1.12',
          defaultAmmo: 'opencode + zcode + qoder',
          activeLanes: [
            {
              lane: 'T254',
              slug: 'bitwarden-client',
              pid: 48210,
              elapsed: '03:15',
              ammo: 'MiniMax-M3.1-Flash',
            },
          ],
        },
      ],
    });

    expect(card.header.title.content).toContain('奥创军团');
    const jsonStr = JSON.stringify(card);
    expect(jsonStr).toContain('PID 48210');
    expect(jsonStr).toContain('bitwarden-client');
  });
});
