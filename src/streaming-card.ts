/**
 * Streaming CardKit card controller.
 *
 * Manages full lifecycle of a streaming LLM response in a Feishu card:
 * idle -> streaming -> completed / error / aborted.
 * Uses FlushController to automatically throttle outbound updates to avoid rate limits.
 */

import { FlushController, DEFAULT_THROTTLE_CONSTANTS, type ThrottleConstants } from "./flush-controller.js";

export type StreamingCardState = "idle" | "streaming" | "completed" | "error" | "aborted";

export interface StreamingCardOptions {
  title?: string;
  initialText?: string;
  throttleMs?: number;
  throttleConstants?: ThrottleConstants;
  onFlush?: (cardPayload: Record<string, any>) => Promise<void>;
}

export class StreamingCardController {
  private state: StreamingCardState = "idle";
  private title: string;
  private reasoningContent: string = "";
  private mainContent: string = "";
  private assistantMessageBase: string = "";
  private errorMessage: string | null = null;
  private readonly flushController: FlushController;
  private readonly throttleMs: number;
  private readonly onFlushCallback?: (cardPayload: Record<string, any>) => Promise<void>;
  private finalizationPromise: Promise<void> | null = null;

  constructor(options: StreamingCardOptions = {}) {
    this.title = options.title ?? "Agent Working...";
    this.mainContent = options.initialText ?? "";
    this.throttleMs = options.throttleMs ?? DEFAULT_THROTTLE_CONSTANTS.defaultThrottleMs;
    this.onFlushCallback = options.onFlush;

    this.flushController = new FlushController(
      async () => {
        if (this.onFlushCallback) {
          const payload = this.buildCardPayload();
          await this.onFlushCallback(payload);
        }
      },
      options.throttleConstants ?? DEFAULT_THROTTLE_CONSTANTS
    );
    this.flushController.setCardMessageReady(true);
  }

  getState(): StreamingCardState {
    return this.state;
  }

  getContent(): string {
    return this.mainContent;
  }

  getReasoningContent(): string {
    return this.reasoningContent;
  }

  /** Last streaming flush failure; cleared by a successful streaming flush. */
  get lastFlushError(): unknown | null {
    return this.flushController.lastFlushError;
  }

  /**
   * Freeze prior prose before a new assistant message, such as after a tool call.
   * Subsequent message snapshots replace only the text after this boundary.
   * Calling this without adding text does not insert an empty paragraph.
   */
  startAssistantMessage(): void {
    if (this.state === "completed" || this.state === "error" || this.state === "aborted") {
      return;
    }
    this.assistantMessageBase = this.mainContent;
  }

  /**
   * Set the cumulative snapshot for the current assistant message.
   * Unlike updateContent(), this preserves prose frozen by startAssistantMessage().
   * Snapshots are authoritative, including corrections to shorter or empty text.
   */
  async updateAssistantMessage(text: string): Promise<void> {
    if (this.state === "completed" || this.state === "error" || this.state === "aborted") {
      return;
    }
    this.state = "streaming";
    this.mainContent = this.assistantMessageBase && text
      ? `${this.assistantMessageBase}\n\n${text}`
      : this.assistantMessageBase || text;
    await this.flushController.throttledUpdate(this.throttleMs);
  }

  /**
   * Append a token chunk to the current message, preserving any explicit boundary.
   */
  async appendToken(token: string): Promise<void> {
    if (this.state === "completed" || this.state === "error" || this.state === "aborted") {
      return;
    }
    this.state = "streaming";
    const startsMessage = this.assistantMessageBase && this.mainContent === this.assistantMessageBase;
    this.mainContent += startsMessage && token ? `\n\n${token}` : token;
    await this.flushController.throttledUpdate(this.throttleMs);
  }

  /**
   * Append reasoning / thinking text chunk.
   */
  async appendReasoningToken(token: string): Promise<void> {
    if (this.state === "completed" || this.state === "error" || this.state === "aborted") {
      return;
    }
    this.state = "streaming";
    this.reasoningContent += token;
    await this.flushController.throttledUpdate(this.throttleMs);
  }

  /**
   * Replace full main content, reset any message boundary, and trigger throttled update.
   */
  async updateContent(text: string): Promise<void> {
    if (this.state === "completed" || this.state === "error" || this.state === "aborted") {
      return;
    }
    this.state = "streaming";
    this.assistantMessageBase = "";
    this.mainContent = text;
    await this.flushController.throttledUpdate(this.throttleMs);
  }

  /**
   * Complete streaming with optional final override text and await delivery.
   * The first terminal operation wins; later terminal calls share its result.
   */
  complete(finalText?: string): Promise<void> {
    return this.finalize("completed", () => {
      if (finalText !== undefined) {
        this.mainContent = finalText;
      }
    });
  }

  /**
   * Abort streaming.
   */
  abort(reason: string = "Aborted by user"): Promise<void> {
    return this.finalize("aborted", () => {
      this.mainContent += `\n\n*(Cancelled: ${reason})*`;
    });
  }

  /**
   * Mark stream as error and flush card.
   */
  error(err: Error | string): Promise<void> {
    return this.finalize("error", () => {
      this.errorMessage = typeof err === "string" ? err : err.message;
    });
  }

  private finalize(
    state: "completed" | "error" | "aborted",
    update: () => void
  ): Promise<void> {
    if (this.finalizationPromise) return this.finalizationPromise;

    this.state = state;
    update();
    const payload = this.buildCardPayload();

    // Stop timers and reflush scheduling before draining the current request.
    // Sending the final snapshot separately keeps it out of the flush mutex's
    // early-return path and prevents an older request from overwriting it.
    this.flushController.complete();
    this.finalizationPromise = this.flushController.waitForFlush().then(async () => {
      if (this.onFlushCallback) {
        await this.onFlushCallback(payload);
      }
    });
    return this.finalizationPromise;
  }

  /**
   * Wait for the current flush, including final delivery once terminating.
   */
  async waitForFlush(): Promise<void> {
    await (this.finalizationPromise ?? this.flushController.waitForFlush());
  }

  /**
   * Build Feishu CardKit 2.0 schema representation.
   */
  buildCardPayload(): Record<string, any> {
    const elements: Array<Record<string, any>> = [];

    // If there is reasoning content, render in quote/callout style
    if (this.reasoningContent.trim()) {
      elements.push({
        tag: "div",
        text: {
          tag: "lark_md",
          content: `**Thinking Process**\n> ${this.reasoningContent.trim().replace(/\n/g, "\n> ")}`,
        },
      });
      elements.push({ tag: "hr" });
    }

    // Main content
    let displayContent = this.mainContent;
    if (this.state === "streaming") {
      displayContent += " ▊"; // Cursor indicator
    } else if (!displayContent && this.state === "idle") {
      displayContent = "*Waiting for response...*";
    }

    elements.push({
      tag: "div",
      text: {
        tag: "lark_md",
        content: displayContent || "*(Empty response)*",
      },
    });

    if (this.errorMessage) {
      elements.push({
        tag: "div",
        text: {
          tag: "lark_md",
          content: `<font color="red">**Error**: ${this.errorMessage}</font>`,
        },
      });
    }

    let headerColor = "blue";
    if (this.state === "completed") headerColor = "green";
    else if (this.state === "error") headerColor = "red";
    else if (this.state === "aborted") headerColor = "grey";

    return {
      config: {
        wide_screen_mode: true,
        update_multi: true,
      },
      header: {
        title: {
          tag: "plain_text",
          content: this.title,
        },
        template: headerColor,
      },
      elements,
    };
  }
}
