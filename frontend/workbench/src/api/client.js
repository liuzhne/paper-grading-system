/**
 * API 客户端。
 *
 * 组织隔离合同（计划 §2.1）：
 * - 每次请求显式携带 X-Organization-ID，**写请求固定发起时所属组织**，
 *   不随用户后续切换而漂移。
 * - 组织上下文有单调递增的 version；切换组织时 abort 全部在途请求，
 *   并丢弃 version 落后的迟到响应，避免旧组织内容回灌新组织界面。
 */

/**
 * 后端注入的运行配置；缺省时回落同源 /api。
 * 沿用旧 SPA 的 `window.__PGS_CONFIG__.apiBase` 约定，两个入口共用同一注入点，
 * 避免非默认 API_PREFIX 下出现两套配置语义。
 */
function apiBase() {
  const injected = globalThis.__PGS_CONFIG__;
  const base = injected && injected.apiBase;
  return typeof base === "string" && base ? base : "/api";
}

/** 组织上下文纪元：每次切换 +1，用于丢弃迟到响应。 */
let contextVersion = 0;
let inflight = new Set();

export function currentContextVersion() {
  return contextVersion;
}

/**
 * 进入新的组织上下文：中止在途请求并作废其响应。
 * 调用方负责在此之后清空 store 中的选中资源与缓存。
 */
export function resetContext() {
  contextVersion += 1;
  for (const controller of inflight) {
    controller.abort();
  }
  inflight = new Set();
  return contextVersion;
}

export class ApiError extends Error {
  constructor(status, detail, payload) {
    super(detail || `请求失败（HTTP ${status}）`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.payload = payload;
  }
}

/** 组织上下文已切换，响应作废。调用方应静默忽略。 */
export class StaleContextError extends Error {
  constructor() {
    super("组织上下文已切换，本次响应已作废");
    this.name = "StaleContextError";
  }
}

/**
 * @param {string} path 以 / 开头的 API 路径（不含 /api 前缀）
 * @param {{method?: string, body?: unknown, organizationId?: string|null,
 *          signal?: AbortSignal, headers?: Record<string,string>}} [options]
 */
export async function request(path, options = {}) {
  const version = contextVersion;
  const controller = new AbortController();
  inflight.add(controller);

  if (options.signal) {
    options.signal.addEventListener("abort", () => controller.abort(), {
      once: true,
    });
  }

  const headers = { ...(options.headers || {}) };
  // 写请求由调用方显式传入 organizationId，锁定发起时的组织。
  if (options.organizationId) {
    headers["X-Organization-ID"] = options.organizationId;
  }

  let body;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  let response;
  try {
    response = await fetch(`${apiBase()}${path}`, {
      method: options.method || "GET",
      credentials: "same-origin",
      headers,
      body,
      signal: controller.signal,
    });
  } catch (error) {
    if (version !== contextVersion) throw new StaleContextError();
    throw error;
  } finally {
    inflight.delete(controller);
  }

  if (version !== contextVersion) throw new StaleContextError();

  if (response.status === 204) return null;

  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (version !== contextVersion) throw new StaleContextError();

  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? payload.detail
        : null;
    throw new ApiError(
      response.status,
      typeof detail === "string" ? detail : null,
      payload,
    );
  }
  return payload;
}

export const api = {
  get: (path, options) => request(path, { ...options, method: "GET" }),
  post: (path, body, options) =>
    request(path, { ...options, method: "POST", body }),
  patch: (path, body, options) =>
    request(path, { ...options, method: "PATCH", body }),
  del: (path, options) => request(path, { ...options, method: "DELETE" }),
};
