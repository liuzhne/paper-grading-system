import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { aiTaskInterval, createVisiblePoller } from "./polling.js";

function fakeDocument() {
  const listeners = new Set();
  return {
    hidden: false,
    addEventListener: (_name, handler) => listeners.add(handler),
    removeEventListener: (_name, handler) => listeners.delete(handler),
    setHidden(value) {
      this.hidden = value;
      listeners.forEach((handler) => handler());
    },
    listenerCount: () => listeners.size,
  };
}

describe("createVisiblePoller", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("polls on an interval while the page is visible", async () => {
    const doc = fakeDocument();
    const tick = vi.fn();
    const poller = createVisiblePoller(tick, 3000, { doc });
    poller.start();
    await vi.advanceTimersByTimeAsync(9000);
    expect(tick).toHaveBeenCalledTimes(3);
    poller.stop();
    await vi.advanceTimersByTimeAsync(9000);
    expect(tick).toHaveBeenCalledTimes(3);
    expect(doc.listenerCount()).toBe(0);
  });

  it("pauses while hidden and refreshes once on return", async () => {
    const doc = fakeDocument();
    const tick = vi.fn();
    const poller = createVisiblePoller(tick, 3000, { doc });
    poller.start();
    doc.setHidden(true);
    await vi.advanceTimersByTimeAsync(30000);
    expect(tick).not.toHaveBeenCalled();

    doc.setHidden(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(tick).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(3000);
    expect(tick).toHaveBeenCalledTimes(2);
    poller.stop();
  });

  it("never overlaps a slow refresh", async () => {
    const doc = fakeDocument();
    let resolve;
    const tick = vi.fn(() => new Promise((done) => { resolve = done; }));
    const poller = createVisiblePoller(tick, 1000, { doc });
    poller.start();
    await vi.advanceTimersByTimeAsync(1000);
    expect(tick).toHaveBeenCalledTimes(1);
    doc.setHidden(true);
    doc.setHidden(false);
    await vi.advanceTimersByTimeAsync(5000);
    expect(tick).toHaveBeenCalledTimes(1);
    resolve();
    await vi.advanceTimersByTimeAsync(1000);
    expect(tick).toHaveBeenCalledTimes(2);
    poller.stop();
  });

  it("keeps polling after a failed refresh", async () => {
    const doc = fakeDocument();
    const tick = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(undefined);
    const poller = createVisiblePoller(tick, 1000, { doc });
    poller.start();
    await vi.advanceTimersByTimeAsync(2000);
    expect(tick).toHaveBeenCalledTimes(2);
    poller.stop();
  });
});

describe("aiTaskInterval", () => {
  it("polls every 2 seconds for the first 30 seconds, then every 5", () => {
    expect(aiTaskInterval(0)).toBe(2000);
    expect(aiTaskInterval(29_999)).toBe(2000);
    expect(aiTaskInterval(30_000)).toBe(5000);
  });
});
