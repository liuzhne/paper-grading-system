import { apiUrl } from "@/api/client.js";

import { APP_URL } from "./links.js";

/**
 * 已登录的用户打开宣传页时直接进工作台（用户决定，2026-10-17）。
 *
 * 只认真实会话：`auth_required === true` 且 `/auth/me` 返回 200。关闭鉴权的开发
 * 模式下 `/auth/me` 对任何人都返回 200，并不代表「已登录」；在那里跳走，本地与
 * 默认 e2e 后端就永远看不到宣传页。
 *
 * 请求失败（未登录 401、网络错误、旧后端）一律留在宣传页：这里拦不住任何东西，
 * 只是省一次点击，宁可不跳也不能把访客挡在空白页上。
 *
 * @param {{ fetchImpl?: typeof fetch, go?: (url: string) => void }} [deps]
 * @returns {Promise<boolean>} 是否发起了跳转
 */
export async function redirectIfSignedIn(deps = {}) {
  const fetchImpl = deps.fetchImpl || globalThis.fetch.bind(globalThis);
  const go = deps.go || ((url) => window.location.replace(url));
  try {
    const response = await fetchImpl(apiUrl("/auth/me"), {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) return false;
    const identity = await response.json();
    if (identity?.auth_required !== true || !identity?.user) return false;
    go(APP_URL);
    return true;
  } catch {
    return false;
  }
}
