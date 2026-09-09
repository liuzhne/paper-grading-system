// @ts-check
/**
 * 没有可用模型时把用户引导去配置 BYOK（用户决定，2026-09-09；D-027、D-028）。
 *
 * 平台未配置默认模型、用户也没绑自己的 API key 连接时，整套评分能力都用不了。
 * 与其让人上传完材料再撞上一个失败，不如在进页面时就说清楚该去哪。
 *
 * **引导不等于把人锁死**：账户与连接页本身、以及平台管理员配置平台模型的运维页，
 * 都必须仍然可达——拦住它们，用户就被挡在一个自己无法解开的门外。
 */

/** 即便没有可用模型也必须可达的页面。 */
const ALWAYS_REACHABLE = new Set([
  // 用户在这里绑定自己的 AI 连接。
  "account",
  // 平台管理员在这里配置平台默认模型。
  "ops",
]);

/**
 * 这次跳转是否应当被引导到「先去配置模型」。
 *
 * @param {{capabilities: any}} session 会话 store
 * @param {{name?: string|symbol|null}} to 目标路由
 * @returns {boolean}
 */
export function requiresModelSetup(session, to) {
  const llm = session?.capabilities?.llm;
  // 能力表还没加载完就跳转，会把正常用户闪到配置页。首屏未知时一律放行，
  // 加载完成后的下一次导航自然会拦。
  if (!llm) return false;
  if (llm.can_use_llm) return false;
  const name = typeof to?.name === "string" ? to.name : "";
  return !ALWAYS_REACHABLE.has(name);
}

/**
 * 引导文案。两条出路都要给：自己配连接，或等平台管理员配默认模型。
 *
 * @param {{capabilities: any, isPlatformAdmin?: boolean}} session
 * @returns {string}
 */
export function modelSetupNotice(session) {
  if (session?.isPlatformAdmin) {
    return "本部署尚未配置可用模型。你可以在「运维与质量」配置平台默认模型，或在「账户与连接」绑定自己的 AI 连接。";
  }
  return "本部署尚未配置可用模型。请在「账户与连接」绑定自己的 AI 连接，或联系平台管理员配置平台默认模型。";
}
