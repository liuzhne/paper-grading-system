/**
 * 部署级的官网设置，经入口页的 `window.__PGS_CONFIG__` 下发。
 *
 * `landingMotion: "reduced"`：宣传页固定为少动效版，不论访客的系统设置。不给值即
 * 正常动效，访客自己开了「减少动态效果」时仍会照顾。
 *
 * 用户决定（2026-10-17）：Vercel 固定少动效（CI 的 Vercel 部署任务在构建前写入），
 * Lightsail 正常动效（FastAPI 不下发这个值）。
 */
export function landingMotionPinnedReduced() {
  return globalThis.__PGS_CONFIG__?.landingMotion === "reduced";
}
