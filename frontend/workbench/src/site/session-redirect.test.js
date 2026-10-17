import { describe, expect, it, vi } from "vitest";

import { redirectIfSignedIn } from "./session-redirect.js";

const reply = (status, body) =>
  vi.fn(async () => ({ ok: status >= 200 && status < 300, status, json: async () => body }));

describe("已登录打开宣传页直接进工作台", () => {
  it("真实会话：跳到工作台", async () => {
    const go = vi.fn();
    const fetchImpl = reply(200, { auth_required: true, user: { id: "u1" }, organization: null });

    expect(await redirectIfSignedIn({ fetchImpl, go })).toBe(true);
    expect(go).toHaveBeenCalledWith("/workbench/");
    expect(fetchImpl).toHaveBeenCalledWith("/api/auth/me", expect.objectContaining({ credentials: "same-origin" }));
  });

  it("未登录（401）：留在宣传页", async () => {
    const go = vi.fn();
    expect(await redirectIfSignedIn({ fetchImpl: reply(401, { detail: "未登录" }), go })).toBe(false);
    expect(go).not.toHaveBeenCalled();
  });

  it("关闭鉴权的开发模式：/auth/me 对谁都是 200，不算已登录", async () => {
    const go = vi.fn();
    const fetchImpl = reply(200, { auth_required: false, user: null, organization: null });

    expect(await redirectIfSignedIn({ fetchImpl, go })).toBe(false);
    expect(go).not.toHaveBeenCalled();
  });

  it("网络失败：留在宣传页，不抛错", async () => {
    const go = vi.fn();
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });

    expect(await redirectIfSignedIn({ fetchImpl, go })).toBe(false);
    expect(go).not.toHaveBeenCalled();
  });
});
