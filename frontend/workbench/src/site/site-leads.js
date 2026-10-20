import { apiUrl } from "@/api/client.js";

/**
 * 提交官网申请（docs/官网申请落库改造方案.md §8.1）。
 *
 * 不经工作台的 API 客户端：它会带组织头与契约版本，而官网访客没有登录、没有组织。
 * 结果分四种，页面据此给出文案；任何失败都要给访客一条退路（发邮件），访客的诉求
 * 不能因为我们的故障丢掉。
 *
 * @param {{ kind: "trial"|"deploy_assessment", email: string, organization_name?: string, note?: string, website?: string }} payload
 * @param {{ fetchImpl?: typeof fetch }} [deps]
 * @returns {Promise<{ ok: true } | { ok: false, reason: "invalid", fields: Record<string, string> } | { ok: false, reason: "rate_limited" | "unavailable" }>}
 */
export async function submitLead(payload, deps = {}) {
  const fetchImpl = deps.fetchImpl || globalThis.fetch.bind(globalThis);
  let response;
  try {
    response = await fetchImpl(apiUrl("/site/leads"), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
    });
  } catch {
    return { ok: false, reason: "unavailable" };
  }
  if (response.status === 202) return { ok: true };
  if (response.status === 422) {
    const body = await response.json().catch(() => null);
    const fields = body?.detail?.fields;
    return {
      ok: false,
      reason: "invalid",
      fields: fields && typeof fields === "object" ? fields : { email: "请检查填写的内容" },
    };
  }
  if (response.status === 429) return { ok: false, reason: "rate_limited" };
  return { ok: false, reason: "unavailable" };
}

/** 失败时的提示。限流与故障都给出发邮件这条退路。 */
export function failureMessage(reason) {
  return reason === "rate_limited" ? "提交太频繁，请稍后再试；也可以直接发邮件给我们：" : "暂时没能提交成功；可以稍后再试，或直接发邮件给我们：";
}
