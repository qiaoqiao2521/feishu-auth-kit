/** Exercise built public entries; in-source tests cannot detect broken bundle imports. */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as legacy from 'feishu-auth-kit';
import * as feishu from 'feishu-auth-kit/feishu';
import * as core from 'feishu-auth-kit/core';
import * as telegram from 'feishu-auth-kit/telegram';
import { TelegramPollStore, TelegramPollingHost } from 'feishu-auth-kit/telegram-host';

assert.equal(legacy.AgentEvent, core.AgentEvent);
assert.equal(legacy.EchoRunner, feishu.EchoRunner);
const message = new core.ChannelMessage({ platform: 'telegram', accountId: '123',
  conversationId: '7', conversationType: 'direct', senderId: '7', messageId: '1', text: 'package smoke' });
assert.equal(new core.EchoRunner().run(core.AgentTurnRequest.fromChannelMessage(message)).outputText,
  'Echo: package smoke');
assert.equal(typeof telegram.TelegramBotClient, 'function');
const store = new TelegramPollStore(':memory:', '123');
store.close();
// Terminate a separate process without close(), then recover its durable claim.
const directory = mkdtempSync(join(tmpdir(), 'channel-kit-package-'));
try {
  for (const phase of ['running', 'sending']) {
    const path = join(directory, `${phase}.sqlite`);
    const crashed = spawnSync(process.execPath, ['--input-type=module', '-e', `
      import { TelegramPollStore } from 'feishu-auth-kit/telegram-host';
      const store = new TelegramPollStore(process.argv[1], '123');
      store.claim('1');
      if (process.argv[2] === 'sending') store.markSending('1');
      process.exit(17);
    `, path, phase], { encoding: 'utf8' });
    assert.equal(crashed.status, 17, 'Child must reach the simulated interruption');
    const recovered = new TelegramPollStore(path, '123');
    try {
      const host = new TelegramPollingHost({ store: recovered, client: {
        getMe: async () => ({ id: '123', isBot: true }),
        getUpdates: async () => [{ update_id: 1 }],
        reply: async () => { throw new Error('Must not resend'); },
      }, runner: { name: 'never', run: () => { throw new Error('Must not re-execute'); } } });
      const [receipt] = await host.pollOnce();
      assert.equal(receipt.disposition, phase === 'sending' ? 'unknown' : 'execution_unknown');
      assert.equal(recovered.getOffset(), '2');
    } finally { recovered.close(); }
  }
} finally { rmSync(directory, { recursive: true, force: true }); }
console.log('Built package subpaths and compatibility identities passed');
