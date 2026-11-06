import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { watch } from "vue";

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
/** 请求编号（发送去重）：UUID 格式。 */
const CLIENT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

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
    expect(calls[0].body).toEqual({ type: "message", text: "第2篇为什么扣分", client_id: expect.stringMatching(CLIENT_ID) });
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

    expect(calls[0].body).toEqual({ type: "message", attachments: { count: 2, names: ["a.docx", "b.docx"] },
      client_id: expect.stringMatching(CLIENT_ID) });
    expect(store.filesByThread["c1:1"]).toEqual(files);
    expect(store.isPending({ id: "start-1" })).toBe(true);
    expect(store.filesForPending()).toEqual(files);
  });

  it("快捷按钮带上意图键，后端按键分派", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    const calls = fakeServer({
      "POST /assistant/conversations/c1/runs": () => [200, { messages: [], pending: null, conversation: conversation() }],
    });

    await store.send("整体分数情况", "query_overview");

    expect(calls[0].body).toEqual({ type: "message", text: "整体分数情况", quick_action: "query_overview",
      client_id: expect.stringMatching(CLIENT_ID) });
  });

  it("附件遇到“要放弃上一步吗”：文件先暂存，不换掉上一步的文件；放弃并开始后才归到新流程", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    const oldFiles = [new File(["o"], "old.docx")];
    store.filesByThread["c1:1"] = oldFiles;
    store.pending = { kind: "confirm_start", card_id: "start-1", thread_id: "c1:1", client: {} };
    const switchCard = { id: "sw-1", type: "confirm_switch_flow", status: "proposed", from_label: "确认开始评分", to_label: "开始评分" };
    const calls = fakeServer({
      "POST /assistant/conversations/c1/runs": (body) => body.type === "message"
        ? [200, { messages: [message("m1", "“确认开始评分”这一步还没完成……", [switchCard])], conversation: conversation(),
          pending: { kind: "confirm_start", card_id: "start-1", thread_id: "c1:1", client: {} } }]
        : [200, { messages: [], conversation: conversation(),
          pending: { kind: "confirm_start", card_id: "start-2", thread_id: "c1:2", client: {} } }],
    });
    const newFiles = [new File(["n"], "new.docx")];

    await store.attachFiles(newFiles);
    expect(store.filesByThread["c1:1"]).toEqual(oldFiles);

    await store.switchFlow(switchCard, "switch");
    expect(calls[1].body).toEqual({ type: "select", card_id: "sw-1", value: { action: "switch" } });
    expect(store.filesByThread["c1:2"]).toEqual(newFiles);
    expect(store.filesByThread["c1:1"]).toEqual(oldFiles);
  });

  it("放弃并开始的请求失败：暂存的文件留着，再点一次照样归到新流程", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    store.pending = { kind: "confirm_start", card_id: "start-1", thread_id: "c1:1", client: {} };
    const switchCard = { id: "sw-1", type: "confirm_switch_flow", status: "proposed" };
    let selects = 0;
    fakeServer({
      "POST /assistant/conversations/c1/runs": (body) => {
        if (body.type === "message") {
          return [200, { messages: [message("m1", "还没完成……", [switchCard])], conversation: conversation(),
            pending: { kind: "confirm_start", card_id: "start-1", thread_id: "c1:1", client: {} } }];
        }
        selects += 1;
        if (selects === 1) return [503, { detail: "暂时不可用" }];
        return [200, { messages: [], conversation: conversation(),
          pending: { kind: "confirm_start", card_id: "start-2", thread_id: "c1:2", client: {} } }];
      },
    });
    const files = [new File(["n"], "new.docx")];

    await store.attachFiles(files);
    await store.switchFlow(switchCard, "switch");
    expect(store.error).toBeTruthy();
    expect(store.filesByThread["c1:2"]).toBeUndefined();

    await store.switchFlow(switchCard, "switch");
    expect(store.filesByThread["c1:2"]).toEqual(files);
  });

  it("暂存的文件只归给收到它们的那张卡片：别的卡片放弃并开始时拿不到", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    store.pending = { kind: "confirm_start", card_id: "start-1", thread_id: "c1:1", client: {} };
    fakeServer({
      "POST /assistant/conversations/c1/runs": (body) => body.type === "message"
        ? [200, { messages: [message("m1", "还没完成……", [{ id: "sw-1", type: "confirm_switch_flow", status: "proposed" }])],
          conversation: conversation(), pending: { kind: "confirm_start", card_id: "start-1", thread_id: "c1:1", client: {} } }]
        : [200, { messages: [], conversation: conversation(),
          pending: { kind: "pick_files", card_id: "pick-2", thread_id: "c1:2", client: {} } }],
    });

    await store.attachFiles([new File(["n"], "new.docx")]);
    await store.switchFlow({ id: "sw-other" }, "switch");

    expect(store.filesByThread["c1:2"]).toBeUndefined();
  });

  it("选“继续上一步”：暂存的文件丢掉，上一步原样", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    store.pending = { kind: "confirm_start", card_id: "start-1", thread_id: "c1:1", client: {} };
    const same = { kind: "confirm_start", card_id: "start-1", thread_id: "c1:1", client: {} };
    fakeServer({
      "POST /assistant/conversations/c1/runs": () => [200, { messages: [], conversation: conversation(), pending: same }],
    });

    await store.attachFiles([new File(["n"], "new.docx")]);
    await store.switchFlow({ id: "sw-1" }, "keep");

    expect(store.pending).toEqual(same);
    expect(Object.keys(store.filesByThread)).toEqual([]);
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

  it("运行锁一直被占（409，等过之后重发仍是 409）：显示原因，不丢已有消息，没发出去的那句标出来", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    store.messages = [message("m1", "之前的消息")];
    fakeServer({
      "POST /assistant/conversations/c1/runs": () => [409, { detail: { code: "ASSISTANT_RUN_IN_PROGRESS", message: "上一步还在处理，请稍候。" } }],
      "GET /assistant/conversations/c1": () => [200, { ...conversation(), running: false, messages: [], pending: null }],
    });

    await store.send("评分进度");

    expect(store.error).toBe("上一步还在处理，请稍候。");
    expect(store.messages.map((item) => [item.role, item.text, item.local || null])).toEqual([
      ["assistant", "之前的消息", null], ["user", "评分进度", "failed"],
    ]);
  });

  it("老师这句话发出时先显示，返回后换成后端存下的那一条", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    store.messages = [message("m1", "之前的消息")];
    let during = [];
    fakeServer({
      "POST /assistant/conversations/c1/runs": (body) => {
        during = store.messages.map((item) => [item.role, item.text, item.local || null]);
        return [200, {
          messages: [{ id: "u1", role: "user", text: body.text, cards: [], created_at: "2026-10-11T00:00:02.5" },
            message("m22", "好的。")],
          pending: null, conversation: conversation(),
        }];
      },
    });

    await store.send("有其他的评分标准吗？");

    expect(during).toEqual([["assistant", "之前的消息", null], ["user", "有其他的评分标准吗？", "sending"]]);
    expect(store.messages.map((item) => item.id)).toEqual(["m1", "u1", "m22"]);
  });

  it("新会话：建会话清空列表后，刚显示的这句话仍在", async () => {
    const store = useAssistantStore();
    let during = [];
    fakeServer({
      "POST /assistant/conversations": () => [201, conversation()],
      "POST /assistant/conversations/c1/runs": () => {
        during = store.messages.map((item) => item.text);
        return [200, { messages: [], pending: null, conversation: conversation() }];
      },
    });

    await store.send("你好");

    expect(during).toEqual(["你好"]);
  });

  it("没发出去的可以重新发送：沿用同一个编号（后端去重，不会多一条），快捷按钮仍带意图键", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    let attempts = 0;
    const calls = fakeServer({
      "POST /assistant/conversations/c1/runs": (body) => {
        attempts += 1;
        // 第一次和自动重发都是 503：标“没有发出去”；老师点“重新发送”时成功。
        if (attempts <= 2) return [503, { detail: "服务暂时不可用" }];
        return [200, { messages: [{ id: "u1", role: "user", text: body.text, client_id: body.client_id, cards: [],
          created_at: "2026-10-11T00:00:05" }], pending: null, conversation: conversation() }];
      },
      "GET /assistant/conversations/c1": () => [200, { ...conversation(), running: false, messages: [], pending: null }],
    });

    await store.send("整体分数情况", "query_overview");
    const failed = store.messages.find((item) => item.local === "failed");
    expect(failed?.text).toBe("整体分数情况");

    await store.resend(failed.id);

    const posts = calls.filter((call) => call.method === "POST").map((call) => call.body);
    expect(posts).toHaveLength(3);
    expect(new Set(posts.map((body) => body.client_id)).size).toBe(1);
    expect(posts[0]).toEqual({ type: "message", text: "整体分数情况", quick_action: "query_overview",
      client_id: expect.stringMatching(CLIENT_ID) });
    expect(store.messages.map((item) => item.id)).toEqual(["u1"]);
    expect(store.error).toBeNull();
  });
});

describe("没有确定结果时，等运行结束、用同一个编号重发一次（发送去重方案 §4.2）", () => {
  const replay = (body, extra = []) => ({ messages: [{ id: "u1", role: "user", text: body.text, client_id: body.client_id,
    cards: [], created_at: "2026-10-11T00:00:02.5" }, ...extra], pending: null, conversation: conversation() });
  const idle = (running = false) => ({ ...conversation(), running, pending: null, messages: [] });

  it("5xx：用同一个编号重发，后端回放第一次的结果，照常显示、不报错", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    let posts = 0;
    const calls = fakeServer({
      "POST /assistant/conversations/c1/runs": (body) => {
        posts += 1;
        return posts === 1 ? [500, { detail: "助手处理失败，请稍后再试。" }] : [200, replay(body, [message("m22", "好的。")])];
      },
      "GET /assistant/conversations/c1": () => [200, idle()],
    });

    await store.send("你好");

    expect(calls.map((call) => call.method)).toEqual(["POST", "GET", "POST"]);
    expect(calls[2].body.client_id).toBe(calls[0].body.client_id);
    expect(store.messages.map((item) => item.id)).toEqual(["u1", "m22"]);
    expect(store.error).toBeNull();
  });

  it("代理的 502：先等这次运行结束（running 变成 false），再重发", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    const store = useAssistantStore();
    store.conversation = conversation();
    let posts = 0;
    let reads = 0;
    fakeServer({
      "POST /assistant/conversations/c1/runs": (body) => {
        posts += 1;
        return posts === 1 ? [502, "Bad Gateway"] : [200, replay(body)];
      },
      "GET /assistant/conversations/c1": () => {
        reads += 1;
        return [200, idle(reads === 1)];
      },
    });

    const sending = store.send("你好");
    await vi.waitFor(() => expect(store.progress?.label).toBe("连接出了问题，正在重新发送…"));
    await vi.advanceTimersByTimeAsync(2000);
    await sending;

    expect([posts, reads]).toEqual([2, 2]);
    expect(store.messages.map((item) => [item.id, item.local || null])).toEqual([["u1", null]]);
  });

  it("网络中断：等运行结束后重发；后端没收到过就照常处理", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    const bodies = [];
    let posts = 0;
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (url, init = {}) => {
      if ((init.method || "GET").toUpperCase() === "POST") {
        posts += 1;
        bodies.push(JSON.parse(init.body));
        if (posts === 1) throw new TypeError("Failed to fetch");
        return new Response(JSON.stringify(replay(bodies[1])), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify(idle()), { status: 200, headers: { "Content-Type": "application/json" } });
    }));

    await store.send("你好");

    expect(bodies.map((body) => body.client_id)).toEqual([bodies[0].client_id, bodies[0].client_id]);
    expect(store.messages.map((item) => item.id)).toEqual(["u1"]);
    expect(store.error).toBeNull();
  });

  it("409：可能是自己第一次的请求还在跑，等它结束再用同一个编号重发", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    let posts = 0;
    const labels = [];
    const stop = watch(() => store.progress?.label, (label) => { if (label) labels.push(label); });
    fakeServer({
      "POST /assistant/conversations/c1/runs": (body) => {
        posts += 1;
        return posts === 1 ? [409, { detail: { code: "ASSISTANT_RUN_IN_PROGRESS", message: "上一步还在处理，请稍候。" } }]
          : [200, replay(body)];
      },
      "GET /assistant/conversations/c1": () => [200, idle()],
    });

    await store.send("你好");
    stop();

    expect(labels).toContain("上一步还在处理，处理完就自动发出…");
    expect(store.messages.map((item) => item.id)).toEqual(["u1"]);
  });

  it("其余 4xx（如 422）：后端确实没收下，不等、不重发，直接标“没有发出去”", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    const calls = fakeServer({
      "POST /assistant/conversations/c1/runs": () => [422, { detail: "请求不合规" }],
    });

    await store.send("你好");

    expect(calls.map((call) => call.method)).toEqual(["POST"]);
    expect(store.messages.map((item) => item.local)).toEqual(["failed"]);
  });

  it("每一句话的编号都不一样", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    const calls = fakeServer({
      "POST /assistant/conversations/c1/runs": () => [200, { messages: [], pending: null, conversation: conversation() }],
    });
    await store.send("一");
    await store.send("二");
    expect(calls[0].body.client_id).not.toBe(calls[1].body.client_id);
  });

  it("附件：重发拿到回放时，只有等待中的卡片是这一轮出的，才把文件交给它的流程", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    const files = [new File(["a"], "a.docx")];
    let own = true;
    let posts = 0;
    fakeServer({
      "POST /assistant/conversations/c1/runs": (body) => {
        posts += 1;
        if (posts % 2 === 1) return [500, { detail: "助手处理失败，请稍后再试。" }];
        const card = { id: "start-1", type: "confirm_start", status: "proposed" };
        return [200, { messages: [{ id: `u${posts}`, role: "user", text: "评分这 1 份文件", client_id: body.client_id,
          cards: [], created_at: "2026-10-11T00:00:02.5" }, message("m22", "开始吗？", own ? [card] : [])],
        // 另一个标签页开的流程：等待中的卡片不在这一轮的消息里。
        pending: { kind: "confirm_start", card_id: own ? "start-1" : "start-9", thread_id: own ? "c1:1" : "c1:9",
          client: { type: "confirm" } }, conversation: conversation() }];
      },
      "GET /assistant/conversations/c1": () => [200, idle()],
    });

    await store.attachFiles(files);
    expect(store.filesByThread["c1:1"]).toEqual(files);

    own = false;
    await store.attachFiles(files);
    expect(store.filesByThread["c1:9"]).toBeUndefined();
  });
});

describe("过程流式", () => {
  const userMessage = (text, at = "2026-10-11T00:00:02.5") => ({ id: "u1", role: "user", text, cards: [], created_at: at });

  /**
   * 假服务器：流式接口按给定的块吐出 SSE（`streams` 给出时每次请求用下一组），会话接口按给定的列表依次返回。
   */
  function streamServer({ chunks = [], hangUp = false, details = [], streams = null }) {
    const calls = [];
    const encoder = new TextEncoder();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (url, init = {}) => {
      const method = (init.method || "GET").toUpperCase();
      const path = String(url).replace(/^\/api/, "");
      calls.push({ method, path, body: init.body ? JSON.parse(init.body) : null });
      if (path.endsWith("/runs/stream")) {
        const current = streams ? streams.shift() : { chunks, hangUp };
        const body = new ReadableStream({
          start(controller) {
            for (const chunk of current.chunks) controller.enqueue(encoder.encode(chunk));
            if (current.hangUp) controller.error(new TypeError("network error"));
            else controller.close();
          },
        });
        return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      if (method === "GET" && path === "/assistant/conversations/c1") {
        const detail = details.length > 1 ? details.shift() : details[0];
        return new Response(JSON.stringify(detail), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify({ detail: `no route ${method} ${path}` }), { status: 404 });
    }));
    return calls;
  }

  const sse = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

  it("开关打开时说话走流式：进度更新到当前这一步，done 照常合并", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    store.settings = { stream_enabled: true };
    const seen = [];
    const calls = streamServer({ chunks: [
      sse("accepted", {}), sse("progress", { label: "正在理解你的问题…", step: 1 }),
      sse("progress", { label: "正在查：可用的评分标准…", step: 2 }),
      sse("done", { messages: [userMessage("有哪些评分标准？"), message("m22", "目前已发布的评分标准有：")],
        pending: null, conversation: conversation() }),
    ] });
    const stop = watch(() => store.progress?.label, (label) => { if (label) seen.push(label); });

    await store.send("有哪些评分标准？");
    stop();

    expect(calls.map((call) => `${call.method} ${call.path}`)).toEqual(["POST /assistant/conversations/c1/runs/stream"]);
    expect(seen).toEqual(["正在理解你的问题…", "正在查：可用的评分标准…"]);
    expect(store.progress).toBeNull();
    expect(store.messages.map((item) => item.id)).toEqual(["u1", "m22"]);
  });

  it("卡片上的操作不走流式", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    store.settings = { stream_enabled: true };
    const calls = fakeServer({
      "POST /assistant/conversations/c1/runs": () => [200, { messages: [], pending: null, conversation: conversation() }],
    });
    await store.select("card-1", { action: "keep" });
    expect(calls.map((call) => call.path)).toEqual(["/assistant/conversations/c1/runs"]);
  });

  it("断线后等这次运行结束，再用同一个编号走流式重发，拿到回放", async () => {
    // 只换 setTimeout：读流时运行环境内部也用计时器，全换掉流就读不动了。
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    const store = useAssistantStore();
    store.conversation = conversation();
    store.settings = { stream_enabled: true };
    const done = { messages: [userMessage("这批写得怎么样"), message("m22", "评完了，概况在右侧。")],
      pending: null, conversation: conversation() };
    const calls = streamServer({
      streams: [
        { chunks: [sse("accepted", {}), sse("progress", { label: "正在查：评分任务概况…", step: 1 })], hangUp: true },
        { chunks: [sse("accepted", {}), sse("done", done)], hangUp: false },
      ],
      details: [{ ...conversation(), running: true, messages: [], pending: null },
        { ...conversation(), running: false, messages: [], pending: null }],
    });

    const sending = store.send("这批写得怎么样");
    await vi.waitFor(() => expect(store.progress?.label).toBe("连接出了问题，正在重新发送…"));
    await vi.advanceTimersByTimeAsync(2000);
    await sending;

    const posts = calls.filter((call) => call.method === "POST");
    expect(posts).toHaveLength(2);
    expect(posts[1].body.client_id).toBe(posts[0].body.client_id);
    expect(calls.filter((call) => call.method === "GET")).toHaveLength(2);
    expect(store.messages.map((item) => item.id)).toEqual(["u1", "m22"]);
    expect(store.error).toBeNull();
    expect(store.progress).toBeNull();
  });
});

describe("助手模型设置", () => {
  it("保存时发 PUT /assistant/settings，成功后关闭设置框", async () => {
    // 2026-10-15：api 上没有 put，点“保存”只显示“保存失败”，请求根本没发出去。
    const saved = { configured: true, model_source: "connection", ai_connection_id: "conn-1", connections: [] };
    const calls = fakeServer({
      "PUT /assistant/settings": () => [200, saved],
    });
    const store = useAssistantStore();
    store.setupOpen = true;

    await store.saveSettings({ model_source: "connection", ai_connection_id: "conn-1" });

    expect(calls).toEqual([
      { method: "PUT", path: "/assistant/settings", body: { model_source: "connection", ai_connection_id: "conn-1" } },
    ]);
    expect(store.settings).toEqual(saved);
    expect(store.setupOpen).toBe(false);
  });
});

describe("工作区（v2 界面改造方案 §3）", () => {
  const snapshot = (id) => message(id, "现在在第 4 步", [{ id: `snap-${id}`, type: "progress_snapshot", steps: [], as_of: "2026-10-29T02:00:00" }]);

  it("新出现的结果卡片打开它的标签；老师一分钟内在任务标签上操作过，只在顶部提示", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    let next = "a1";
    fakeServer({
      "POST /assistant/conversations/c1/runs": () => [200, { messages: [snapshot(next)], pending: null, conversation: conversation() }],
    });

    await store.send("现在进展到哪了？", "query_progress");
    expect(store.ws.tab).toBe("snap-a1");

    store.openStep(null);
    store.noteTaskEdit();
    next = "a22";
    await store.send("现在进展到哪了？", "query_progress");
    expect(store.ws.tab).toBe("task");
    expect(store.ws.toast).toBe("snap-a22");
  });

  it("流程里的结果卡片（带 step）是任务标签的入口，不另开标签；窄屏工作区收着时不弹出来", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    store.ws.mode = "closed";
    store.ws.narrow = true;
    fakeServer({
      "POST /assistant/conversations/c1/runs": () => [200, {
        messages: [message("a1", "评分完成。", [{ id: "o1", type: "overview", batch_id: "b1", step: "report" }]), snapshot("a22")],
        pending: null, conversation: conversation(),
      }],
    });
    await store.send("现在进展到哪了？", "query_progress");
    expect(store.ws.mode).toBe("closed");
    expect(store.ws.tab).toBe("task");
  });

  it("“开始评分”“换评分标准”只恢复正在等的那一步，别的等待不受影响", async () => {
    const store = useAssistantStore();
    store.conversation = conversation();
    const calls = fakeServer({
      "POST /assistant/conversations/c1/runs": () => [200, { messages: [], pending: null, conversation: conversation() }],
    });

    store.pending = { kind: "pick_files", card_id: "files-1" };
    await store.answerConfirm("confirm");
    expect(calls).toHaveLength(0);
    await store.changeRubric("r2");
    expect(calls[0].body).toEqual({ type: "resume", card_id: "files-1", value: { action: "change_rubric", rubric_id: "r2" } });

    store.pending = { kind: "confirm_start", card_id: "start-1" };
    await store.answerConfirm("confirm");
    expect(calls[1].body).toEqual({ type: "resume", card_id: "start-1", value: { action: "confirm" } });
  });
});

it("新流程开始（计划卡片）时打开工作区的任务标签", async () => {
  setActivePinia(createPinia());
  const store = useAssistantStore();
  store.conversation = conversation();
  store.ws.mode = "closed";
  fakeServer({
    "POST /assistant/conversations/c1/runs": () => [200, {
      messages: [message("a1", "这次评分分 7 步完成", [{ id: "plan-1", type: "plan", status: "running", steps: [] }])],
      pending: null, conversation: conversation(),
    }],
  });
  await store.send("开始评分", "start_grading");
  expect(store.ws.mode).toBe("open");
  expect(store.ws.tab).toBe("task");
});
