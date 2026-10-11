import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAssistantStore } from "./assistant.js";

/**
 * 评分助手 store 是薄客户端：只发 `runs`、渲染返回、做浏览器才能做的事（选文件、上传、轮询）。
 * 用一个按“方法 路径”路由的假服务器替代 fetch，记录每一次调用。
 */
function fakeServer(routes) {
  const calls = [];
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (url, init = {}) => {
    const method = (init.method || "GET").toUpperCase();
    const path = String(url).replace(/^\/api/, "");
    const body = init.body && typeof init.body === "string" ? JSON.parse(init.body) : null;
    calls.push({ method, path, body });
    const handler = routes[`${method} ${path}`];
    if (!handler) return new Response(JSON.stringify({ detail: `no route ${method} ${path}` }), { status: 404 });
    const [status, payload] = await handler(body, calls);
    return new Response(payload === undefined ? "" : JSON.stringify(payload), {
      status, headers: { "Content-Type": "application/json" },
    });
  }));
  return calls;
}

const conversation = (focus = {}) => ({
  id: "c1", title: "对话", focus, created_at: "2026-10-11T00:00:00", updated_at: "2026-10-11T00:00:00",
});
const message = (id, text, cards = []) => ({ id, role: "assistant", text, cards, created_at: `2026-10-11T00:00:0${id.length}` });

beforeEach(() => {
  setActivePinia(createPinia());
  globalThis.__PGS_CONFIG__ = undefined;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("assistant store", () => {
  it("发送一句话：只调用 runs，按返回渲染消息与工作区", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    const calls = fakeServer({
      "POST /assistant/conversations/c1/runs": (body) => [200, {
        messages: [
          { id: "u1", role: "user", text: body.text, cards: [], created_at: "2026-10-11T00:00:01" },
          message("m1", "第 2 份《李四》：总分 78。", [{ id: "r1", type: "paper_result", status: "info", batch_id: "b1", paper_id: "p2", run_id: "run2" }]),
        ],
        pending: null,
        conversation: conversation({ batch_id: "b1", workspace: "/batches/b1/grade?paper=p2", workspace_rev: 1 }),
      }],
    });

    await store.send("第2篇为什么扣分");

    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual(["POST /assistant/conversations/c1/runs"]);
    expect(calls[0].body).toEqual({ type: "message", text: "第2篇为什么扣分" });
    expect(store.messages.map((item) => item.id)).toEqual(["u1", "m1"]);
    expect(store.workspacePath).toBe("/batches/b1/grade?paper=p2");
  });

  it("附件：先报数量与文件名，文件留在本页内存，按流程线程归档", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    const calls = fakeServer({
      "POST /assistant/conversations/c1/runs": () => [200, {
        messages: [message("m1", "用《标准》评这 2 份文件……开始吗？", [{ id: "start-1", type: "confirm_start", status: "proposed" }])],
        pending: { kind: "confirm_start", card_id: "start-1", thread_id: "c1:1", client: { type: "confirm" } },
        conversation: conversation(),
      }],
    });
    const files = [new File(["a"], "a.docx"), new File(["b"], "b.docx")];

    await store.attachFiles(files);

    expect(calls[0].body).toEqual({ type: "message", attachments: { count: 2, names: ["a.docx", "b.docx"] } });
    expect(store.filesByThread["c1:1"]).toEqual(files);
    expect(store.isPending({ id: "start-1" })).toBe(true);
    expect(store.filesForPending()).toEqual(files);
  });

  it("确认开始后流程要求上传：用同一批文件直传，再带论文编号恢复", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    const file = new File(["a"], "a.docx");
    store.filesByThread["c1:1"] = [file];
    store.pending = { kind: "confirm_start", card_id: "start-1", thread_id: "c1:1", client: { type: "confirm" } };
    const bodies = [];
    const calls = fakeServer({
      "POST /assistant/conversations/c1/runs": (body) => {
        bodies.push(body);
        if (bodies.length === 1) {
          return [200, { messages: [], conversation: conversation(),
            pending: { kind: "upload_papers", card_id: "start-1", thread_id: "c1:1", client: { type: "upload_papers", batch_id: "b1", file_count: 1 } } }];
        }
        return [200, { messages: [], conversation: conversation(),
          pending: { kind: "wait_job", card_id: "job-1", thread_id: "c1:1", client: { type: "watch_job", batch_id: "b1", job_id: "j1" } } }];
      },
      "GET /system/capabilities": () => [200, { upload: { provider: "local", max_size_mb: 20, accepted_extensions: [".docx", ".pdf"] } }],
      "POST /papers/upload": () => [200, { id: "p1", status: "parsed" }],
      "GET /batches/b1/score-jobs/latest": () => [200, { id: "j1", status: "running" }],
    });

    await store.resume("start-1", { action: "confirm" });
    await vi.waitFor(() => expect(store.pending.kind).toBe("wait_job"));

    expect(bodies).toEqual([
      { type: "resume", card_id: "start-1", value: { action: "confirm" } },
      { type: "resume", card_id: "start-1", value: { paper_ids: ["p1"] } },
    ]);
    expect(calls.filter((call) => call.path === "/papers/upload")).toHaveLength(1);
    store.stopWatchers();
  });

  it("轮询发现作业结束后通知图继续；是否真的结束由后端核对", async () => {
    vi.useFakeTimers();
    const store = useAssistantStore();
    const calls = fakeServer({
      "GET /assistant/conversations/c1": () => [200, {
        ...conversation(), messages: [],
        pending: { kind: "wait_job", card_id: "job-1", thread_id: "c1:1", client: { type: "watch_job", batch_id: "b1", job_id: "j1" } },
      }],
      "GET /batches/b1/score-jobs/latest": () => [200, { id: "j1", status: "completed" }],
      "POST /assistant/conversations/c1/runs": () => [200, { messages: [message("m9", "评分完成。")], pending: null, conversation: conversation() }],
    });

    await store.open("c1");
    await vi.runOnlyPendingTimersAsync();

    const resumes = calls.filter((call) => call.method === "POST");
    expect(resumes).toHaveLength(1);
    expect(resumes[0].body).toEqual({ type: "resume", card_id: "job-1", value: { event: "job_finished" } });
    expect(store.pending).toBeNull();
    expect(store.jobs.b1.status).toBe("completed");
  });

  it("后端拒绝（例如运行锁 409）时显示原因，不丢已有消息", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    store.messages = [message("m1", "之前的消息")];
    fakeServer({
      "POST /assistant/conversations/c1/runs": () => [409, { detail: { code: "ASSISTANT_RUN_IN_PROGRESS", message: "上一步还在处理，请稍候。" } }],
    });

    await store.send("评分进度");

    expect(store.error).toBe("上一步还在处理，请稍候。");
    expect(store.messages.map((item) => item.id)).toEqual(["m1"]);
  });
});
