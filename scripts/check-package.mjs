/** Exercise unchanged public Feishu exports and the optional SDK composition example. */
import assert from 'node:assert/strict';
import { AgentEvent, AgentTurnRequest, EchoRunner, FeishuMessageContext } from 'feishu-auth-kit';
import { createTelegramPilot } from '../examples/chat-sdk/telegram.mjs';

const context = new FeishuMessageContext({ app_id: 'cli_test', chat_id: 'oc_test',
  chat_type: 'p2p', message_id: 'om_test', sender_open_id: 'ou_test', text: 'package smoke' });
const request = AgentTurnRequest.fromMessageContext(context);
assert.equal(request.toDict().schema, 'feishu-auth-kit.agent-turn-request.v1');
assert.equal(new EchoRunner().run(request).outputText, 'Echo: package smoke');
assert.equal(AgentEvent.status('ok').kind, 'status');
const pilot = createTelegramPilot({ accountId: '123', botToken: '123:FAKE_TEST_TOKEN',
  secretToken: 'FAKE_WEBHOOK_SECRET', userName: 'testbot', grants: [{ userId: '42', chatId: '42' }] });
assert.equal(typeof pilot.webhooks.telegram, 'function');
console.log('Legacy package and direct Chat SDK composition imports passed (no network)');
