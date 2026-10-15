import { afterEach, describe, it, expect, vi } from "vitest";
import { StreamingCardController } from "../src/streaming-card.js";

function deferred() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("StreamingCardController", () => {
  it("initializes with default options and generates valid card structure", () => {
    const controller = new StreamingCardController({
      title: "Task in Progress",
      initialText: "Hello",
    });

    expect(controller.getState()).toBe("idle");
    expect(controller.getContent()).toBe("Hello");

    const payload = controller.buildCardPayload();
    expect(payload.header.title.content).toBe("Task in Progress");
    expect(payload.header.template).toBe("blue");
    expect(payload.elements.length).toBeGreaterThan(0);
    expect(payload.elements[0].text.content).toContain("Hello");
  });

  it("handles streaming tokens and changes state to streaming", async () => {
    const onFlush = vi.fn().mockResolvedValue(undefined);
    const controller = new StreamingCardController({
      title: "LLM Bot",
      onFlush,
    });

    await controller.appendToken("First chunk");
    expect(controller.getState()).toBe("streaming");
    expect(controller.getContent()).toBe("First chunk");

    await controller.appendReasoningToken("Analyzing intent...");
    expect(controller.getReasoningContent()).toBe("Analyzing intent...");

    const payload = controller.buildCardPayload();
    // In streaming mode, cursor indicator should be appended
    expect(payload.elements.some((e: any) => e.text?.content?.includes("First chunk ▊"))).toBe(true);
    expect(payload.elements.some((e: any) => e.text?.content?.includes("Thinking Process"))).toBe(true);
  });

  it("completes streaming and flushes final payload with green header", async () => {
    let lastPayload: any = null;
    const onFlush = vi.fn().mockImplementation(async (payload) => {
      lastPayload = payload;
    });

    const controller = new StreamingCardController({
      title: "Finishing",
      onFlush,
    });

    await controller.appendToken("Thinking...");
    await controller.complete("Final answer here.");

    expect(controller.getState()).toBe("completed");
    expect(controller.getContent()).toBe("Final answer here.");
    expect(lastPayload).not.toBeNull();
    expect(lastPayload.header.template).toBe("green");
    expect(lastPayload.elements.some((e: any) => e.text?.content === "Final answer here.")).toBe(true);
  });

  it("handles abort and error states appropriately", async () => {
    let lastPayload: any = null;
    const onFlush = vi.fn().mockImplementation(async (payload) => {
      lastPayload = payload;
    });

    const controller1 = new StreamingCardController({ title: "To Abort", onFlush });
    await controller1.abort("user pressed stop");
    expect(controller1.getState()).toBe("aborted");
    expect(lastPayload.header.template).toBe("grey");
    expect(controller1.getContent()).toContain("user pressed stop");

    const controller2 = new StreamingCardController({ title: "To Error", onFlush });
    await controller2.error(new Error("Network disconnect"));
    expect(controller2.getState()).toBe("error");
    expect(lastPayload.header.template).toBe("red");
    expect(lastPayload.elements.some((e: any) => e.text?.content?.includes("Network disconnect"))).toBe(true);
  });

  it.each(["completed", "error", "aborted"] as const)(
    "delivers the %s snapshot after an in-flight update and shares finalization",
    async (state) => {
      vi.useFakeTimers();
      const streamingRequest = deferred();
      const finalRequest = deferred();
      const onFlush = vi.fn()
        .mockImplementationOnce(() => streamingRequest.promise)
        .mockImplementationOnce(() => finalRequest.promise);
      const controller = new StreamingCardController({ onFlush, throttleMs: 0 });

      const streaming = controller.appendToken("draft");
      expect(onFlush).toHaveBeenCalledTimes(1);
      await controller.appendToken(" queued");

      const terminal = state === "completed"
        ? controller.complete("final answer")
        : state === "error"
          ? controller.error("network failed")
          : controller.abort("stop requested");
      let finalized = false;
      void terminal.then(() => { finalized = true; });
      const waiting = controller.waitForFlush();
      let finishedWaiting = false;
      void waiting.then(() => { finishedWaiting = true; });

      // The first terminal event owns both the snapshot and completion promise.
      expect(controller.complete("late overwrite")).toBe(terminal);
      expect(controller.error("late error")).toBe(terminal);
      expect(controller.abort("late abort")).toBe(terminal);
      await controller.appendToken("late token");
      await controller.appendReasoningToken("late reasoning");
      await controller.updateContent("late replacement");
      expect(controller.getState()).toBe(state);
      expect(controller.getReasoningContent()).toBe("");
      expect(onFlush).toHaveBeenCalledTimes(1);
      expect(finalized).toBe(false);

      streamingRequest.resolve();
      await streaming;
      expect(onFlush).toHaveBeenCalledTimes(2);
      const payload = onFlush.mock.calls[1][0];
      expect(payload.header.template).toBe(
        state === "completed" ? "green" : state === "error" ? "red" : "grey"
      );
      const contents = payload.elements.map((element: any) => element.text?.content).join("\n");
      expect(contents).not.toContain("▊");
      expect(contents).not.toContain("late");
      if (state === "completed") expect(controller.getContent()).toBe("final answer");
      if (state === "error") expect(contents).toContain("network failed");
      if (state === "aborted") expect(contents).toContain("Cancelled: stop requested");

      await vi.advanceTimersByTimeAsync(1000);
      expect(onFlush).toHaveBeenCalledTimes(2);
      expect(vi.getTimerCount()).toBe(0);
      expect(finalized).toBe(false);
      expect(finishedWaiting).toBe(false);

      finalRequest.resolve();
      await terminal;
      await waiting;
      expect(finalized).toBe(true);
      expect(finishedWaiting).toBe(true);
    }
  );

  it("cancels a deferred streaming update before sending the final snapshot", async () => {
    vi.useFakeTimers();
    const onFlush = vi.fn().mockResolvedValue(undefined);
    const controller = new StreamingCardController({ onFlush });

    await controller.appendToken("draft");
    expect(vi.getTimerCount()).toBe(1);
    await controller.complete("final answer");
    await vi.advanceTimersByTimeAsync(1000);

    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(onFlush.mock.calls[0][0].elements[0].text.content).toBe("final answer");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("still delivers the final snapshot if the in-flight streaming request fails", async () => {
    const streamingRequest = deferred();
    const onFlush = vi.fn()
      .mockImplementationOnce(() => streamingRequest.promise)
      .mockResolvedValue(undefined);
    const controller = new StreamingCardController({ onFlush, throttleMs: 0 });

    const streaming = controller.appendToken("draft");
    const streamingFailure = expect(streaming).rejects.toThrow("streaming failed");
    const terminal = controller.complete("final answer");
    streamingRequest.reject(new Error("streaming failed"));
    await streamingFailure;
    await terminal;

    expect(onFlush).toHaveBeenCalledTimes(2);
    expect(onFlush.mock.calls[1][0].elements[0].text.content).toBe("final answer");
  });

  it("shares final delivery failures and keeps the chosen terminal state frozen", async () => {
    vi.useFakeTimers();
    const failure = new Error("final delivery failed");
    const onFlush = vi.fn().mockRejectedValue(failure);
    const controller = new StreamingCardController({ onFlush });
    await controller.appendToken("draft");

    const terminal = controller.complete("final answer");
    expect(controller.error("late error")).toBe(terminal);
    await expect(terminal).rejects.toBe(failure);
    await expect(controller.waitForFlush()).rejects.toBe(failure);
    expect(controller.abort()).toBe(terminal);
    await controller.appendToken("late token");
    await vi.advanceTimersByTimeAsync(1000);

    expect(controller.getState()).toBe("completed");
    expect(controller.getContent()).toBe("final answer");
    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });
});
