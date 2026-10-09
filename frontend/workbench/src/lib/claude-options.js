/**
 * Claude（anthropic_messages）连接专属的两项设置：思考强度与结构化输出。
 * 账户页与运维页（平台模型）共用；后端按协议校验，别的协议带上这两项会被拒绝。
 */

export const CLAUDE_PROTOCOL = "anthropic_messages";

/** 「模型默认」= 不发送（Opus 5.5 默认中等）。更高的 xhigh / max 只经 API 设置。 */
export const EFFORT_CHOICES = [
  ["", "模型默认"],
  ["low", "低"],
  ["medium", "中"],
  ["high", "高"],
];

/** 「自动」：只有 api.anthropic.com 开启；Bedrock 与第三方兼容端点关闭。 */
export const STRUCTURED_OUTPUT_CHOICES = [
  ["", "自动"],
  ["json_schema", "开启"],
  ["off", "关闭"],
];

/**
 * 与后端离线识别一致：/messages 结尾、路径含 /anthropic 段、或 api.anthropic.com。
 * 只用于决定是否显示 Claude 设置；最终协议仍以服务端为准。
 * @param {string} url
 */
export function looksLikeClaudeUrl(url) {
  let parsed;
  try {
    parsed = new URL(String(url || "").trim());
  } catch {
    return false;
  }
  const path = parsed.pathname.replace(/\/+$/, "").toLowerCase();
  const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
  return (
    path.endsWith("/messages") ||
    path.split("/").includes("anthropic") ||
    host === "api.anthropic.com" ||
    host.endsWith(".api.anthropic.com")
  );
}

/**
 * 把两项 Claude 设置合进 provider_options。留空或不是 Claude 时移除这两项——
 * PATCH 是整体替换，其它已有选项原样保留。
 * @param {Record<string, unknown> | null | undefined} options
 * @param {{ effort?: string, structured_output?: string }} values
 * @param {boolean} isClaude
 */
export function withClaudeOptions(options, values, isClaude) {
  const next = { ...(options || {}) };
  for (const key of /** @type {const} */ (["effort", "structured_output"])) {
    const value = String(values?.[key] ?? "").trim();
    if (isClaude && value) next[key] = value;
    else delete next[key];
  }
  return next;
}
