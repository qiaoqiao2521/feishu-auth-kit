import { describe, it, expect, vi } from "vitest";
import {
  InteractiveActionDispatcher,
  type CardActionContext,
} from "../src/interactive-dispatcher.js";

describe("InteractiveActionDispatcher", () => {
  it("parses context correctly from Feishu webhook card callback event", () => {
    const rawEvent = {
      open_id: "ou_tester1",
      open_message_id: "om_msg123",
      open_chat_id: "oc_chat456",
      action: {
        tag: "button",
        value: {
          action: "approve",
          task_id: "task-001",
        },
      },
    };

    const ctx = InteractiveActionDispatcher.parseContext(rawEvent);
    expect(ctx.openId).toBe("ou_tester1");
    expect(ctx.messageId).toBe("om_msg123");
    expect(ctx.chatId).toBe("oc_chat456");
    expect(ctx.tag).toBe("button");
    expect(ctx.actionValue).toEqual({ action: "approve", task_id: "task-001" });
  });

  it("dispatches to registered action handler by actionName", async () => {
    const dispatcher = new InteractiveActionDispatcher();
    const approveHandler = vi.fn().mockReturnValue({
      toast: { type: "success", content: "Task approved" },
      card: { config: { wide_screen_mode: true }, elements: [] },
    });

    dispatcher.onAction("approve", approveHandler);

    const event = {
      open_id: "ou_user1",
      open_message_id: "om_1",
      action: {
        tag: "button",
        value: { action: "approve" },
      },
    };

    const response = await dispatcher.dispatch(event);
    expect(approveHandler).toHaveBeenCalledTimes(1);
    expect(response.toast?.content).toBe("Task approved");
    expect(response.card).toBeDefined();
  });

  it("routes by action_id if specified", async () => {
    const dispatcher = new InteractiveActionDispatcher();
    const retryHandler = vi.fn().mockReturnValue({
      toast: { type: "info", content: "Retrying..." },
    });

    dispatcher.onAction("retry_btn", retryHandler);

    const event = {
      open_id: "ou_user1",
      open_message_id: "om_1",
      action: {
        tag: "button",
        value: { action_id: "retry_btn" },
      },
    };

    const response = await dispatcher.dispatch(event);
    expect(retryHandler).toHaveBeenCalledTimes(1);
    expect(response.toast?.content).toBe("Retrying...");
  });

  it.each([false, true])("routes form_submit by form_name with SDK envelope=%s", async (wrapped) => {
    const formValue = { answer: "继续", choices: ["one", "two"] };
    const body = {
      operator: { open_id: "ou_operator", user_id: "user_operator" },
      context: { open_message_id: "om_form", open_chat_id: "oc_form" },
      action: { tag: "form_submit", form_name: "question.submit", form_value: formValue },
    };
    const event = wrapped
      ? { schema: "2.0", header: { event_type: "card.action.trigger" }, event: body }
      : body;
    const handler = vi.fn((ctx: CardActionContext) => ({
      toast: { type: "success" as const, content: String(ctx.formValue?.answer) },
    }));
    const dispatcher = new InteractiveActionDispatcher().onAction("question.submit", handler);

    const response = await dispatcher.dispatch(event);

    expect(response.toast?.content).toBe("继续");
    expect(handler).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      openId: "ou_operator",
      userId: "user_operator",
      messageId: "om_form",
      chatId: "oc_form",
      actionName: "question.submit",
      actionValue: {},
      formValue,
      rawEvent: event,
    }));
  });

  it("routes named submit buttons and preserves submitted fields", async () => {
    const handler = vi.fn();
    const dispatcher = new InteractiveActionDispatcher().onAction("review.submit", handler);
    await dispatcher.dispatch({
      operator: { open_id: "ou_user" },
      action: { tag: "button", name: "review.submit", form_value: { feedback: "LGTM" } },
    });
    expect(handler).toHaveBeenCalledWith(expect.objectContaining({
      actionName: "review.submit", formValue: { feedback: "LGTM" },
    }));
  });

  it("prefers explicit button routes over names regardless of registration order", async () => {
    const handlers = { explicit: vi.fn(), id: vi.fn(), name: vi.fn(), form: vi.fn() };
    const dispatcher = new InteractiveActionDispatcher()
      .onAction("form", handlers.form)
      .onAction("name", handlers.name)
      .onAction("id", handlers.id)
      .onAction("explicit", handlers.explicit);
    await dispatcher.dispatch({
      action: {
        tag: "form_submit", name: "name", form_name: "form",
        value: { action: "explicit", action_id: "id" },
      },
    });
    expect(handlers.explicit).toHaveBeenCalledOnce();
    expect(handlers.id).not.toHaveBeenCalled();
    expect(handlers.name).not.toHaveBeenCalled();
    expect(handlers.form).not.toHaveBeenCalled();
  });

  it("keeps Schema 2 user_id distinct when no open_id is provided", () => {
    const ctx = InteractiveActionDispatcher.parseContext({ operator: { user_id: "u_only" } });
    expect(ctx.openId).toBe("");
    expect(ctx.userId).toBe("u_only");
  });

  it("ignores malformed values and form names on other action tags", async () => {
    const handler = vi.fn();
    const dispatcher = new InteractiveActionDispatcher().onAction("question.submit", handler);
    const event = {
      action: { tag: "button", form_name: "question.submit", value: [], form_value: "invalid" },
    };
    expect(InteractiveActionDispatcher.parseContext(event)).toMatchObject({ actionValue: {}, formValue: {} });
    await dispatcher.dispatch(event);
    expect(handler).not.toHaveBeenCalled();
  });

  it("accepts existing pre-parsed contexts without the added optional fields", async () => {
    const handler = vi.fn();
    const dispatcher = new InteractiveActionDispatcher().onAction("retry", handler);
    const ctx: CardActionContext = {
      openId: "ou_user", messageId: "om_1", tag: "button", actionValue: { action: "retry" }, rawEvent: {},
    };
    await dispatcher.dispatch(ctx);
    expect(handler).toHaveBeenCalledExactlyOnceWith(ctx);
  });

  it("falls back to default fallback handler when no matches", async () => {
    const dispatcher = new InteractiveActionDispatcher();
    const event = {
      open_id: "ou_user1",
      open_message_id: "om_1",
      action: {
        tag: "button",
        value: { action: "unknown_cmd" },
      },
    };

    const response = await dispatcher.dispatch(event);
    expect(response.toast?.type).toBe("info");
    expect(response.toast?.content).toBe("Action acknowledged");
  });

  it("supports custom fallback handler", async () => {
    const dispatcher = new InteractiveActionDispatcher();
    dispatcher.setFallback((ctx) => ({
      toast: { type: "warning", content: `Unrecognized action: ${JSON.stringify(ctx.actionValue)}` },
    }));

    const event = {
      open_id: "ou_user1",
      open_message_id: "om_1",
      action: {
        tag: "button",
        value: { custom: 123 },
      },
    };

    const response = await dispatcher.dispatch(event);
    expect(response.toast?.type).toBe("warning");
    expect(response.toast?.content).toContain("Unrecognized action");
  });
});
