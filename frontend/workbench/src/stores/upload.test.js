import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { requiresOwnConnection, useUploadStore } from "./upload.js";

/**
 * 上传编排（前端 v2 计划 §5-E）。
 *
 * 两条最要紧的约束：
 * - **逐文件隔离**：一个失败不撤销其它成功文件，也不阻断后续文件。
 * - **不偷偷回落**：直传未配置时必须报错，不能改走函数转发把大文件塞进
 *   Serverless 请求体（那正是 FUNCTION_PAYLOAD_TOO_LARGE 的成因）。
 */
describe("upload store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    globalThis.__PGS_CONFIG__ = undefined;
  });

  function file(name, size = 1024) {
    const blob = new Blob(["x".repeat(size)], { type: "application/pdf" });
    return new File([blob], name, { type: "application/pdf" });
  }

  function jsonResponse(body, status = 200) {
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  }

  const LOCAL_CAPS = {
    upload: { provider: "local", max_size_mb: 50, tus_threshold_mb: 6, accepted_extensions: [".docx", ".pdf"] },
  };

  it("按能力表读取上传限制，不写死在前端", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => jsonResponse(LOCAL_CAPS)));
    const store = useUploadStore();

    await store.loadCapabilities();

    expect(store.provider).toBe("local");
    expect(store.maxSizeMb).toBe(50);
    expect(store.acceptedExtensions).toEqual([".docx", ".pdf"]);
  });

  it("超过上限的文件在上传前就被拒绝，并说明原因", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => jsonResponse(LOCAL_CAPS)));
    const store = useUploadStore();
    await store.loadCapabilities();

    const rejected = store.stage([file("big.pdf", 10)], { sizeOverride: 60 * 1024 * 1024 });

    expect(rejected[0].status).toBe("rejected");
    expect(rejected[0].error).toContain("50");
  });

  it("扩展名不在白名单内的文件被拒绝", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => jsonResponse(LOCAL_CAPS)));
    const store = useUploadStore();
    await store.loadCapabilities();

    const staged = store.stage([file("notes.txt")]);

    expect(staged[0].status).toBe("rejected");
    expect(staged[0].error).toContain("docx");
  });

  it("local provider 逐文件上传，一个失败不影响其它", async () => {
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url) => {
        if (String(url).includes("capabilities")) return jsonResponse(LOCAL_CAPS);
        call += 1;
        if (call === 2) return jsonResponse({ detail: "解析失败" }, 400);
        return jsonResponse({ id: `p${call}`, status: "parsed" });
      }),
    );
    const store = useUploadStore();
    await store.loadCapabilities();
    store.stage([file("a.pdf"), file("b.pdf"), file("c.pdf")]);

    await store.uploadAll("b1");

    const states = store.queue.map((entry) => entry.status);
    expect(states).toEqual(["done", "failed", "done"]);
    expect(store.doneCount).toBe(2);
    expect(store.failedCount).toBe(1);
  });

  it("失败项可单独重试，不重传已成功的文件", async () => {
    let attempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url) => {
        if (String(url).includes("capabilities")) return jsonResponse(LOCAL_CAPS);
        attempts += 1;
        if (attempts === 1) return jsonResponse({ detail: "网络抖动" }, 500);
        return jsonResponse({ id: "p1", status: "parsed" });
      }),
    );
    const store = useUploadStore();
    await store.loadCapabilities();
    store.stage([file("a.pdf")]);
    await store.uploadAll("b1");
    expect(store.queue[0].status).toBe("failed");

    await store.retryFailed("b1");

    expect(store.queue[0].status).toBe("done");
  });

  it("supabase 直传未配置时报错，绝不回落到函数转发", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url) => {
        const path = String(url);
        if (path.includes("capabilities")) {
          return jsonResponse({
            upload: { provider: "supabase", max_size_mb: 50, tus_threshold_mb: 6, accepted_extensions: [".pdf"] },
          });
        }
        if (path.includes("direct-upload-intents")) {
          return jsonResponse({ detail: "私有文件存储尚未配置" }, 503);
        }
        // 回落到 /papers/upload 就是 bug，这里断言它从未被调用。
        throw new Error("不应回落到函数转发上传");
      }),
    );
    const store = useUploadStore();
    await store.loadCapabilities();
    store.stage([file("a.pdf")]);

    await store.uploadAll("b1");

    expect(store.queue[0].status).toBe("failed");
    expect(store.queue[0].error).toContain("存储");
  });

  it("进度分别报告上传与解析的完成数", async () => {
    let seq = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url) => {
        if (String(url).includes("capabilities")) return jsonResponse(LOCAL_CAPS);
        seq += 1;
        return jsonResponse({ id: `p${seq}`, status: "parsed" });
      }),
    );
    const store = useUploadStore();
    await store.loadCapabilities();
    store.stage([file("a.pdf"), file("b.pdf")]);

    await store.uploadAll("b1");

    expect(store.doneCount).toBe(2);
    expect(store.uploadedPaperIds).toHaveLength(2);
  });

  it("同一 paper ID 重复返回时不重复计数", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url) =>
        String(url).includes("capabilities")
          ? jsonResponse(LOCAL_CAPS)
          : jsonResponse({ id: "p1", status: "parsed" }),
      ),
    );
    const store = useUploadStore();
    await store.loadCapabilities();
    store.stage([file("a.pdf"), file("b.pdf")]);

    await store.uploadAll("b1");

    expect(store.uploadedPaperIds).toEqual(["p1"]);
  });

  it("清空队列不影响已归档材料的 ID", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url) =>
        String(url).includes("capabilities")
          ? jsonResponse(LOCAL_CAPS)
          : jsonResponse({ id: "p1", status: "parsed" }),
      ),
    );
    const store = useUploadStore();
    await store.loadCapabilities();
    store.stage([file("a.pdf")]);
    await store.uploadAll("b1");

    const archived = [...store.uploadedPaperIds];
    store.clearQueue();

    expect(store.queue).toEqual([]);
    expect(archived).toHaveLength(1);
  });
});

describe("upload store · multipart 契约", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    globalThis.__PGS_CONFIG__ = undefined;
  });

  function file(name) {
    return new File([new Blob(["x"])], name, { type: "application/pdf" });
  }

  function jsonResponse(body, status = 200) {
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  }

  it("local 上传把 batch_id 放进表单，而不是查询参数", async () => {
    /**
     * 后端 papers.upload_paper 用 Form(...) 接收 batch_id。写成 query 会以
     * 422 missing batch_id 失败——本地部署的上传会全线不可用。
     */
    let captured = null;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url, init) => {
        if (String(url).includes("capabilities")) {
          return jsonResponse({
            upload: { provider: "local", max_size_mb: 50, tus_threshold_mb: 6, accepted_extensions: [".pdf"] },
          });
        }
        captured = { url: String(url), init };
        return jsonResponse({ id: "p1", status: "parsed" });
      }),
    );
    const store = useUploadStore();
    await store.loadCapabilities();
    store.stage([file("a.pdf")]);

    await store.uploadAll("batch-42");

    expect(captured.url).toBe("/api/papers/upload");
    expect(captured.url).not.toContain("batch_id=");
    expect(captured.init.body).toBeInstanceOf(FormData);
    expect(captured.init.body.get("batch_id")).toBe("batch-42");
    // Content-Type 必须交给浏览器补 boundary。
    expect(captured.init.headers["Content-Type"]).toBeUndefined();
  });
});

describe("取消调度与刷新恢复", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    globalThis.__PGS_CONFIG__ = undefined;
  });

  function jsonResponse(body, status = 200) {
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  }

  function file(name) {
    return new File(["x"], name, { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
  }

  it("取消后不再调度剩余文件，已开始的那个保留结果", async () => {
    let served = 0;
    vi.stubGlobal("fetch", () => {
      served += 1;
      return jsonResponse({ id: `p${served}` });
    });
    const store = useUploadStore();
    store.provider = "local";
    store.acceptedExtensions = [".docx"];
    store.stage([file("a.docx"), file("b.docx"), file("c.docx")]);

    // 第一个文件一开始上传就取消：后面两个不该再发请求。
    const run = store.uploadAll("b1");
    store.cancel();
    await run;

    expect(served).toBe(1);
    expect(store.queue[0].status).toBe("done");
    // 取消不是失败：剩下的仍是待上传，重试入口不该把它们当成错误。
    expect(store.queue[1].status).toBe("pending");
    expect(store.queue[2].status).toBe("pending");
  });

  it("取消后可以再次开始，从没传的那个继续", async () => {
    let served = 0;
    vi.stubGlobal("fetch", () => {
      served += 1;
      return jsonResponse({ id: `p${served}` });
    });
    const store = useUploadStore();
    store.provider = "local";
    store.acceptedExtensions = [".docx"];
    store.stage([file("a.docx"), file("b.docx")]);

    const run = store.uploadAll("b1");
    store.cancel();
    await run;
    await store.uploadAll("b1");

    // 已归档的不重传：总共两份材料，两次请求。
    expect(served).toBe(2);
    expect(store.uploadedPaperIds).toEqual(["p1", "p2"]);
  });

  it("刷新后按服务端文件状态恢复，已归档的不重传", async () => {
    vi.stubGlobal("fetch", (url) => {
      if (String(url).includes("/papers?batch_id=")) {
        return jsonResponse([
          { id: "p1", file_name: "a.docx", status: "parsed" },
          { id: "p2", file_name: "b.docx", status: "uploaded" },
        ]);
      }
      return jsonResponse({});
    });
    const store = useUploadStore();

    await store.restoreFromServer("b1");

    expect(store.uploadedPaperIds).toEqual(["p1", "p2"]);
    expect(store.queue.map((e) => e.name)).toEqual(["a.docx", "b.docx"]);
    // 服务端已有的条目标为 done：它们已经归档，重传会产生重复对象。
    expect(store.queue.every((e) => e.status === "done")).toBe(true);
  });

  it("恢复失败时不假装队列是空的", async () => {
    vi.stubGlobal("fetch", () => jsonResponse({ detail: "boom" }, 500));
    const store = useUploadStore();

    await expect(store.restoreFromServer("b1")).rejects.toThrow();
    expect(store.uploadedPaperIds).toEqual([]);
  });
});

describe("能力表迟到时的浏览器侧预检", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("能力表未到时先入队，到了之后补判并拒掉不合规的文件", async () => {
    const store = useUploadStore();

    // 用户手快：能力表还在路上就选了文件。此前这种情况下**所有文件一律放行**，
    // 浏览器侧预检等于没有——一个 .txt 或一个超限大文件会一路走到上传。
    store.stage([
      { name: "good.docx", size: 2048 },
      { name: "bad.txt", size: 16 },
    ]);
    expect(store.queue.map((e) => e.status)).toEqual(["pending", "pending"]);

    vi.stubGlobal("fetch", () =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            upload: {
              provider: "local",
              max_size_mb: 50,
              tus_threshold_mb: 6,
              accepted_extensions: [".docx", ".pdf"],
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    await store.loadCapabilities();

    const byName = Object.fromEntries(store.queue.map((e) => [e.name, e]));
    expect(byName["good.docx"].status).toBe("pending");
    expect(byName["bad.txt"].status).toBe("rejected");
    expect(byName["bad.txt"].error).toContain(".docx");
  });

  it("补判只碰仍在等待的条目，不改已上传或失败的", async () => {
    const store = useUploadStore();
    store.stage([{ name: "done.txt", size: 16 }]);
    store.queue[0].status = "uploaded";

    vi.stubGlobal("fetch", () =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            upload: {
              provider: "local",
              max_size_mb: 50,
              tus_threshold_mb: 6,
              accepted_extensions: [".docx"],
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    await store.loadCapabilities();

    // 已经传上去的东西不能因为一次迟到的能力表被标成 rejected。
    expect(store.queue[0].status).toBe("uploaded");
  });
});

describe("挂载顺序：清理不能发生在加载之后", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("能力表加载期间入队的文件不会被随后的 reset 清掉", async () => {
    const store = useUploadStore();
    let resolveCaps;
    vi.stubGlobal(
      "fetch",
      () =>
        new Promise((resolve) => {
          resolveCaps = () =>
            resolve(
              new Response(
                JSON.stringify({
                  upload: {
                    provider: "local",
                    max_size_mb: 50,
                    tus_threshold_mb: 6,
                    accepted_extensions: [".docx"],
                  },
                }),
                { status: 200, headers: { "Content-Type": "application/json" } },
              ),
            );
        }),
    );

    // 页面正确的做法是先清干净再加载。反过来的话，用户在等待期间选的文件
    // 会被这次 reset 静默清掉——界面上文件凭空消失，没有任何解释。
    store.reset();
    const loading = store.loadCapabilities();
    store.stage([{ name: "bad.txt", size: 16 }]);
    resolveCaps();
    await loading;

    expect(store.queue).toHaveLength(1);
    expect(store.queue[0].status).toBe("rejected");
  });
});

describe("新建任务必须绑定模型来源", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("有平台模型时不强制选连接", () => {
    const llm = {
      platform_model_available: true,
      has_own_connection: false,
      can_use_llm: true,
    };

    expect(requiresOwnConnection(llm)).toBe(false);
  });

  it("没有平台模型时必须选自己的连接", () => {
    const llm = {
      platform_model_available: false,
      has_own_connection: true,
      can_use_llm: true,
    };

    // 平台没配模型时，不绑连接就评分等于评 Mock 假分（D-027）。
    expect(requiresOwnConnection(llm)).toBe(true);
  });

  it("能力表未知时不下结论", () => {
    expect(requiresOwnConnection(null)).toBe(false);
  });
});
