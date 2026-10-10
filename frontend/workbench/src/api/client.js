/**
 * API 客户端。
 *
 * 组织隔离合同（计划 §2.1）：
 * - 每次请求显式携带 X-Organization-ID，**写请求固定发起时所属组织**，
 *   不随用户后续切换而漂移。
 * - 组织上下文有单调递增的 version；切换组织时 abort 全部在途请求，
 *   并丢弃 version 落后的迟到响应，避免旧组织内容回灌新组织界面。
 */

import { API_CONTRACT_HEADER, API_CONTRACT_VERSION } from "./contract.js";

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

export function apiUrl(path) {
  return `${apiBase()}${path}`;
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

/**
 * 版本守卫（方案第 5 节）：后端响应的契约版本与本页面构建时的不一致，说明这是旧页面。
 * 记下来，在下一次写操作或路由切换之前静默刷新，旧页面就不会去调已停用的接口。
 */
let contractStale = false;
/** @type {string|null} */
let serverContract = null;
const RELOADED_KEY = "pgs-contract-reloaded";

export function contractIsStale() {
  return contractStale;
}

/** 测试用：恢复为未发现版本变化。 */
export function resetContractGuard() {
  contractStale = false;
  serverContract = null;
}

function noteContract(response) {
  const value = response?.headers?.get?.(API_CONTRACT_HEADER);
  if (value && value !== API_CONTRACT_VERSION) {
    contractStale = true;
    serverContract = value;
  }
}

/** 本标签页是否已为这个后端版本刷新过一次（刷新后仍不一致说明部署本身不一致）。 */
function alreadyReloadedFor(version) {
  try {
    return globalThis.sessionStorage?.getItem(RELOADED_KEY) === version;
  } catch {
    return false;
  }
}

function markReloadedFor(version) {
  try {
    globalThis.sessionStorage?.setItem(RELOADED_KEY, version);
  } catch {
    // 隐私模式等拿不到存储：照常刷新，只是失去“每个版本只刷新一次”的保护。
  }
}

/**
 * 页面已过期时刷新并返回 true。
 *
 * 每个后端版本在一个标签页里最多刷新一次：如果刷新后的页面仍与后端不一致（前端产物
 * 没有随后端一起部署），继续刷新只会让每次操作都白点，不如照常请求，由后端返回明确错误。
 * @param {() => void} [reload]
 */
export function reloadIfStale(reload = () => globalThis.location?.reload()) {
  if (!contractStale || !serverContract) return false;
  if (alreadyReloadedFor(serverContract)) {
    console.warn(`页面接口版本 ${API_CONTRACT_VERSION} 与服务端 ${serverContract} 不一致，刷新后仍未更新；请检查前端产物是否随后端一起部署。`);
    contractStale = false;
    return false;
  }
  markReloadedFor(serverContract);
  reload();
  return true;
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
 * @param {{method?: string, body?: unknown, formData?: FormData,
 *          organizationId?: string|null, signal?: AbortSignal,
 *          headers?: Record<string,string>}} [options]
 */
export async function request(path, options = {}) {
  const method = options.method || "GET";
  if (method !== "GET" && reloadIfStale()) {
    // 写操作发出之前刷新：旧页面不该带着旧契约写数据。调用方按“上下文已切换”静默处理。
    throw new StaleContextError();
  }
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
  if (options.formData !== undefined) {
    // multipart：绝不手工设置 Content-Type，浏览器要自己补 boundary。
    body = options.formData;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  let response;
  try {
    response = await fetch(`${apiBase()}${path}`, {
      method,
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
  noteContract(response);

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
      typeof detail === "string" ? detail :
        detail && typeof detail.message === "string"
          ? [detail.message, typeof detail.user_action === "string" ? detail.user_action : ""].filter(Boolean).join(" ")
          : null,
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
