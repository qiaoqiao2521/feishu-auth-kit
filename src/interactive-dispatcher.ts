/**
 * Interactive Action Dispatcher for Feishu CardKit card callbacks.
 *
 * Dispatches `card.action.trigger` events to registered handlers based on
 * action tag, action ID, or custom predicates. Supports immediate card update responses,
 * toast messages, and fallback handlers.
 */

export interface CardActionContext {
  openId: string;
  userId?: string;
  messageId: string;
  chatId?: string;
  tag: string;
  actionValue: Record<string, any>;
  /** Effective route: value.action, value.action_id, name, then form_submit form_name. */
  actionName?: string;
  /** Submitted field values, including checkbox arrays and text inputs. */
  formValue?: Record<string, any>;
  rawEvent: Record<string, any>;
}

function asRecord(value: unknown): Record<string, any> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, any>
    : undefined;
}

function firstString(...values: unknown[]): string | undefined {
  return values.find((value): value is string => typeof value === "string" && !!value.trim());
}

export interface CardActionToast {
  type: "info" | "success" | "warning" | "danger";
  content: string;
}

export interface CardActionResponse {
  toast?: CardActionToast;
  card?: Record<string, any>;
}

export type CardActionHandler = (
  ctx: CardActionContext
) => Promise<CardActionResponse | void> | CardActionResponse | void;

export type MatchPredicate = (ctx: CardActionContext) => boolean;

export class InteractiveActionDispatcher {
  private handlers: Array<{
    predicate: MatchPredicate;
    handler: CardActionHandler;
  }> = [];

  private fallbackHandler: CardActionHandler = () => {
    return {
      toast: {
        type: "info",
        content: "Action acknowledged",
      },
    };
  };

  /**
   * Register a handler for a button action or form name.
   */
  onAction(actionName: string, handler: CardActionHandler): this {
    return this.register(
      (ctx) => (ctx.actionName ?? firstString(ctx.actionValue?.action, ctx.actionValue?.action_id)) === actionName,
      handler
    );
  }

  /**
   * Register a handler for a specific tag (e.g. tag === 'button')
   */
  onTag(tag: string, handler: CardActionHandler): this {
    return this.register((ctx) => ctx.tag === tag, handler);
  }

  /**
   * Register a custom predicate matcher.
   */
  register(predicate: MatchPredicate, handler: CardActionHandler): this {
    this.handlers.push({ predicate, handler });
    return this;
  }

  /**
   * Set fallback handler when no registered handlers match.
   */
  setFallback(handler: CardActionHandler): this {
    this.fallbackHandler = handler;
    return this;
  }

  /**
   * Parse raw incoming Feishu webhook/websocket event payload into CardActionContext.
   */
  static parseContext(event: Record<string, any>): CardActionContext {
    // The SDK passes the event body directly; webhook ingress often passes its envelope.
    const body = asRecord(event.event) ?? event;
    const action = asRecord(body.action) ?? {};
    const context = asRecord(body.context) ?? {};
    const operator = asRecord(body.operator) ?? {};
    const senderId = asRecord(asRecord(body.sender)?.sender_id) ?? {};
    const openMessageId = firstString(context.open_message_id, body.open_message_id) ?? "";
    const openId = firstString(operator.open_id, body.open_id, context.open_id, senderId.open_id) ?? "";
    // user_id and open_id are different namespaces; preserve each without aliasing them.
    const userId = firstString(operator.user_id, body.user_id, context.user_id);
    const chatId = firstString(context.open_chat_id, body.open_chat_id);
    const tag = firstString(action.tag) ?? "button";
    const actionValue = asRecord(action.value) ?? {};
    const actionName = firstString(
      actionValue.action,
      actionValue.action_id,
      action.name,
      tag === "form_submit" ? action.form_name : undefined,
    );

    return {
      openId,
      userId,
      messageId: openMessageId,
      chatId,
      tag,
      actionValue,
      actionName,
      formValue: asRecord(action.form_value) ?? {},
      rawEvent: event,
    };
  }

  /**
   * Dispatch an action trigger event.
   */
  async dispatch(eventOrContext: Record<string, any> | CardActionContext): Promise<CardActionResponse> {
    const ctx: CardActionContext =
      "openId" in eventOrContext && "actionValue" in eventOrContext
        ? (eventOrContext as CardActionContext)
        : InteractiveActionDispatcher.parseContext(eventOrContext);

    for (const entry of this.handlers) {
      if (entry.predicate(ctx)) {
        const res = await entry.handler(ctx);
        if (res) return res;
        return {
          toast: { type: "success", content: "Action executed" },
        };
      }
    }

    const fallbackRes = await this.fallbackHandler(ctx);
    if (fallbackRes) return fallbackRes;
    return {
      toast: { type: "info", content: "Action processed" },
    };
  }
}
