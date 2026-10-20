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

/**
 * 官网申请表单开没开（docs/官网申请落库改造方案.md）。
 *
 * 只有 FastAPI 在 `SITE_LEADS_ENABLED` 打开时往入口页里写 `siteLeads: true`。Vercel 的
 * 首页是静态文件，拿不到这个值，表单不出现——它连的是切流量后要删除的旧库。
 */
export function siteLeadsEnabled() {
  return globalThis.__PGS_CONFIG__?.siteLeads === true;
}

/** 申请记录的保留天数，随开关一起下发；缺省与后端默认一致。 */
export function siteLeadsRetentionDays() {
  const days = Number(globalThis.__PGS_CONFIG__?.siteLeadsRetentionDays);
  return Number.isInteger(days) && days > 0 ? days : 180;
}
