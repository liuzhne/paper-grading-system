/**
 * 进度轮询：页面可见时按间隔调用 `tick`；页面隐藏（切到别的标签页、锁屏）时暂停，
 * 回到前台立即刷新一次再继续。几百人同时在线时，后台标签页不再持续打接口。
 *
 * 用 setTimeout 串行而不是 setInterval：一次请求变慢时不会叠出并发请求。
 *
 * @param {() => unknown} tick 每轮执行的刷新（可返回 Promise）
 * @param {number | ((elapsedMs: number) => number)} interval 间隔毫秒；函数形式按已轮询时长给出间隔
 * @param {{ doc?: Document, timers?: { setTimeout: typeof setTimeout, clearTimeout: typeof clearTimeout }, now?: () => number }} [env]
 * @returns {{ start(): void, stop(): void, refresh(): Promise<void> }}
 */
export function createVisiblePoller(tick, interval, env = {}) {
  const doc = env.doc ?? globalThis.document;
  const timers = env.timers ?? globalThis;
  const now = env.now ?? (() => Date.now());
  let timer = null;
  let running = false;
  let inFlight = null;
  let startedAt = now();

  const hidden = () => Boolean(doc?.hidden);
  const delay = () => (typeof interval === "function" ? interval(now() - startedAt) : interval);

  function clear() {
    if (timer != null) {
      timers.clearTimeout(timer);
      timer = null;
    }
  }

  function schedule() {
    clear();
    if (!running || hidden()) return;
    timer = timers.setTimeout(() => {
      timer = null;
      run().finally(schedule);
    }, delay());
  }

  function run() {
    if (inFlight) return inFlight;
    inFlight = Promise.resolve()
      .then(() => tick())
      .catch(() => {})
      .finally(() => {
        inFlight = null;
      });
    return inFlight;
  }

  function onVisibility() {
    if (!running) return;
    clear();
    if (!hidden()) run().finally(schedule);
  }

  return {
    start() {
      if (running) return;
      running = true;
      startedAt = now();
      doc?.addEventListener?.("visibilitychange", onVisibility);
      schedule();
    },
    stop() {
      running = false;
      clear();
      doc?.removeEventListener?.("visibilitychange", onVisibility);
    },
    refresh() {
      return run();
    },
  };
}

/** AI 任务的轮询节奏：前 30 秒每 2 秒一次，之后每 5 秒一次。 */
export function aiTaskInterval(elapsedMs) {
  return elapsedMs < 30_000 ? 2_000 : 5_000;
}
