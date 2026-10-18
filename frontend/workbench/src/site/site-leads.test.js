import { describe, expect, it, vi } from "vitest";

import { failureMessage, submitLead } from "./site-leads.js";

const reply = (status, body = {}) => vi.fn(async () => ({ status, ok: status < 300, json: async () => body }));

describe("提交官网申请", () => {
  it("202 即成功；只发同源 JSON", async () => {
    const fetchImpl = reply(202, { status: "received" });

    expect(await submitLead({ kind: "trial", email: "a@b.cn" }, { fetchImpl })).toEqual({ ok: true });
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("/api/site/leads");
    expect(init).toMatchObject({ method: "POST", credentials: "same-origin" });
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({ kind: "trial", email: "a@b.cn" });
  });

  it("422 带回逐字段的中文提示", async () => {
    const fetchImpl = reply(422, { detail: { code: "invalid_fields", fields: { email: "请填写有效的邮箱" } } });

    expect(await submitLead({ kind: "trial", email: "x" }, { fetchImpl })).toEqual({
      ok: false,
      reason: "invalid",
      fields: { email: "请填写有效的邮箱" },
    });
  });

  it("限流、关闭、故障与断网都有明确的失败原因", async () => {
    expect(await submitLead({}, { fetchImpl: reply(429) })).toEqual({ ok: false, reason: "rate_limited" });
    expect(await submitLead({}, { fetchImpl: reply(404) })).toEqual({ ok: false, reason: "unavailable" });
    expect(await submitLead({}, { fetchImpl: reply(503) })).toEqual({ ok: false, reason: "unavailable" });
    const offline = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    expect(await submitLead({}, { fetchImpl: offline })).toEqual({ ok: false, reason: "unavailable" });
  });

  it("失败提示都给出发邮件这条退路", () => {
    expect(failureMessage("rate_limited")).toContain("发邮件");
    expect(failureMessage("unavailable")).toContain("发邮件");
  });
});
