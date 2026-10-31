import { describe, expect, it } from 'vitest';
import {
  AgentEvent, AgentTurnRequest, ChannelMessage, EchoRunner, buildCodexPrompt, buildSessionKey,
} from '../src/core/index.js';
import {
  AgentEvent as LegacyEvent, AgentTurnRequest as LegacyRequest,
  EchoRunner as LegacyEchoRunner, buildCodexPrompt as buildLegacyPrompt,
} from '../src/agent-runtime.js';
import { FeishuMessageContext } from '../src/message-context.js';

function message() {
  return new ChannelMessage({
    platform: 'telegram', accountId: 'bot-1', conversationId: 'chat-1', conversationType: 'direct',
    senderId: 'user-1', messageId: 'message-1', text: 'Hello from Telegram',
  });
}

describe('platform-independent runner contracts', () => {
  it('uses normalized input and isolated sessions without any transport dependency', () => {
    const input = message();
    const request = AgentTurnRequest.fromChannelMessage(input);
    const result = new EchoRunner().run(request);
    expect(request.sessionId).toBe(buildSessionKey(input));
    expect(request.toDict().schema).toBe('channel-kit.agent-turn-request.v1');
    expect(request.toDict().context).toEqual(input.toDict());
    expect(result.toDict().schema).toBe('channel-kit.agent-turn-result.v1');
    expect(result.outputText).toBe('Echo: Hello from Telegram');
    expect(result.metadata.message_id).toBe('message-1');
  });

  it('builds a generic prompt and preserves explicit prompt and runtime instructions', () => {
    const request = AgentTurnRequest.fromChannelMessage(message(), {
      prompt: '', systemPrompt: '  Follow host policy.  ',
    });
    const prompt = buildCodexPrompt(request);
    expect(prompt).toContain('Channel message context:');
    expect(prompt).toContain('- platform: telegram');
    expect(prompt).toContain('- account_id: bot-1');
    expect(prompt).toContain('Runtime instructions:\nFollow host policy.');
    expect(prompt).not.toContain('Feishu');
    expect(request.prompt).toBe('');
  });

  it('retains event class identity through the compatibility facade', () => {
    expect(AgentEvent).toBe(LegacyEvent);
    expect(AgentEvent.assistantMessage('hello')).toBeInstanceOf(LegacyEvent);
  });

  it('preserves the legacy request/result schemas, null session and exact prompt', () => {
    const context = new FeishuMessageContext({
      event_type: 'im.message.receive_v1', message_id: 'om-1', chat_id: 'oc-1',
      sender_open_id: 'ou-1', text: '  legacy   text  ',
    });
    const request = LegacyRequest.fromMessageContext(context);
    const result = new LegacyEchoRunner().run(request);
    expect(request.context).toBe(context);
    expect(request.sessionId).toBeNull();
    expect(request.toDict().schema).toBe('feishu-auth-kit.agent-turn-request.v1');
    expect(result.toDict().schema).toBe('feishu-auth-kit.agent-turn-result.v1');
    expect(buildLegacyPrompt(request)).toBe([
      'You are running inside a Feishu native agent runtime adapter.',
      'Return the final reply text for the Feishu user.', '', 'Feishu message context:',
      '- event_type: im.message.receive_v1', '- message_id: om-1', '- chat_id: oc-1',
      '- sender_open_id: ou-1', '', 'User message:', 'legacy text',
    ].join('\n'));
  });
});
