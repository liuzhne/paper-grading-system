import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, request, resetContext, ApiError, StaleContextError } from "./client.js";

/** 组织隔离合同（计划 §2.1）的回归测试。 */
describe("api client", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    globalThis.__PGS_CONFIG__ = undefined;
  });

  function respond(body, init = {}) {
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  }

  it("默认回落同源 /api，注入配置后使用注入前缀", async () => {
    // 每次调用都要新建 Response：body 只能被读取一次。
    const fetchMock = vi.fn().mockImplementation(() => respond({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await api.get("/auth/me");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/auth/me");

    globalThis.__PGS_CONFIG__ = { apiBase: "/custom-api" };
    await api.get("/auth/me");
    expect(fetchMock.mock.calls[1][0]).toBe("/custom-api/auth/me");
  });

  it("写请求锁定发起时的组织，不随当前选中组织漂移", async () => {
    const fetchMock = vi.fn().mockResolvedValue(respond({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await api.post("/batches", { name: "x" }, { organizationId: "org-a" });

    expect(fetchMock.mock.calls[0][1].headers["X-Organization-ID"]).toBe("org-a");
  });

  it("组织切换后，迟到响应被丢弃而不是回灌界面", async () => {
    let release;
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => pending),
    );

    const inflight = request("/batches", { organizationId: "org-a" });
    // 用户在响应回来之前切到了另一个组织。
    resetContext();
    release(respond([{ id: "batch-from-org-a" }]));

    await expect(inflight).rejects.toBeInstanceOf(StaleContextError);
  });

  it("切换组织会中止在途请求", async () => {
    const seen = [];
    vi.stubGlobal(
      "fetch",
      // 模拟真实 fetch：收到 abort 信号时以 AbortError 拒绝。
      vi.fn().mockImplementation((_url, init) => {
        seen.push(init.signal);
        return new Promise((_resolve, reject) => {
          init.signal.addEventListener(
            "abort",
            () => reject(new DOMException("Aborted", "AbortError")),
            { once: true },
          );
        });
      }),
    );

    const inflight = request("/batches");
    resetContext();

    expect(seen[0].aborted).toBe(true);
    await expect(inflight).rejects.toBeInstanceOf(StaleContextError);
  });

  it("错误响应带出 status 与 detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(respond({ detail: "无权访问该组织" }, { status: 403 })),
    );

    await expect(api.get("/organizations")).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
      detail: "无权访问该组织",
    });
  });

  it("204 返回 null 而不是解析失败", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));

    await expect(api.post("/auth/logout")).resolves.toBeNull();
  });

  it("ApiError 与 StaleContextError 可被调用方区分", () => {
    expect(new ApiError(500, null, null)).toBeInstanceOf(Error);
    expect(new StaleContextError()).toBeInstanceOf(Error);
  });
});
