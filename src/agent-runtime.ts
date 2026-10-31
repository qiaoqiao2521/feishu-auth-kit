/** Feishu compatibility facade. New transports import runners from ./core/index.js. */
import {
  AgentEvent,
  AgentTurnRequest as CoreAgentTurnRequest,
  AgentTurnResult as CoreAgentTurnResult,
  EchoRunner as CoreEchoRunner,
  CodexCliRunner as CoreCodexCliRunner,
} from './core/agent-runtime.js';
import { FeishuMessageContext } from './message-context.js';

export { AgentEvent };
export type { AgentEventKind } from './core/agent-runtime.js';

export class AgentTurnRequest extends CoreAgentTurnRequest {
  declare readonly context: FeishuMessageContext;

  constructor(options: {
    context: FeishuMessageContext;
    prompt: string;
    systemPrompt?: string | null;
    sessionId?: string | null;
    metadata?: Record<string, any>;
  }) { super(options); }

  static fromMessageContext(
    context: FeishuMessageContext,
    options?: { prompt?: string | null; systemPrompt?: string | null; sessionId?: string | null; metadata?: Record<string, any> },
  ): AgentTurnRequest {
    return new AgentTurnRequest({
      context,
      prompt: options?.prompt ?? context.promptText(),
      systemPrompt: options?.systemPrompt,
      sessionId: options?.sessionId,
      metadata: options?.metadata,
    });
  }

  override toDict(): Record<string, any> {
    return { ...super.toDict(), schema: 'feishu-auth-kit.agent-turn-request.v1' };
  }
}

export class AgentTurnResult extends CoreAgentTurnResult {
  override toDict(): Record<string, any> {
    return { ...super.toDict(), schema: 'feishu-auth-kit.agent-turn-result.v1' };
  }
}

export interface AgentRunner {
  name: string;
  run(request: AgentTurnRequest): AgentTurnResult | Promise<AgentTurnResult>;
}

export class EchoRunner extends CoreEchoRunner implements AgentRunner {
  override run(request: AgentTurnRequest): AgentTurnResult {
    return new AgentTurnResult(super.run(request));
  }
}

export function buildCodexPrompt(request: AgentTurnRequest): string {
  const parts = [
    'You are running inside a Feishu native agent runtime adapter.',
    'Return the final reply text for the Feishu user.',
    '',
    'Feishu message context:',
    `- event_type: ${request.context.eventType || '(unknown)'}`,
    `- message_id: ${request.context.messageId || '(unknown)'}`,
    `- chat_id: ${request.context.chatId || '(unknown)'}`,
    `- sender_open_id: ${request.context.senderOpenId || '(unknown)'}`,
  ];
  if (request.sessionId) {
    parts.push(`- session_id: ${request.sessionId}`);
  }
  if (request.systemPrompt) {
    parts.push('', 'Runtime instructions:', request.systemPrompt.trim());
  }
  parts.push('', 'User message:', request.prompt);
  return parts.join('\n').trim();
}


export class CodexCliRunner extends CoreCodexCliRunner implements AgentRunner {
  protected override buildPrompt(request: CoreAgentTurnRequest): string {
    return buildCodexPrompt(request as AgentTurnRequest);
  }

  override run(request: AgentTurnRequest): AgentTurnResult {
    return new AgentTurnResult(super.run(request));
  }
}
