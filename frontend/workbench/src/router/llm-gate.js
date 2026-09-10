// @ts-check
/**
 * 模型可用性：判定、拦截（D-027、D-028、D-032）。
 *
 * **判定、呈现、拦截是三件事**：
 * - 这里的 `requiresModelSetup` 只回答「需不需要配置」；
 * - `ModelSetupDialog` 负责说清楚，**可关闭**——否则它会盖住配置表单；
 * - `blockedByMissingModel` 负责拦住功能页。
 *
 * 上一版把拦截也交给了弹窗，于是**关掉弹窗后全站畅通**，而那些页面上的动作在后端
 * 一律失败。用户看到的是「能进去，但什么都做不成」。关掉弹窗只表示不想再看那段
 * 说明，不表示已经配好模型。
 */

/** 即便没有可用模型也必须可达：那正是解开这件事的两个地方。 */
const CONFIG_ROUTES = new Set(["account", "ops"]);

/**
 * 这个会话现在需不需要先去配置模型。
 *
 * @param {{capabilities: any}} session 会话 store
 * @returns {boolean}
 */
export function requiresModelSetup(session) {
  const llm = session?.capabilities?.llm;
  // 能力表还没加载完就判定，会让正常用户先看到一次误报。首屏未知时一律放行，
  // 加载完成后拦截与弹窗自然会生效。
  if (!llm) return false;
  return !llm.can_use_llm;
}

/**
 * 这次跳转是否应当被拦下。
 *
 * @param {{capabilities: any}} session
 * @param {{name?: string|symbol|null}} to 目标路由
 * @returns {boolean}
 */
export function blockedByMissingModel(session, to) {
  if (!requiresModelSetup(session)) return false;
  const name = typeof to?.name === "string" ? to.name : "";
  return !CONFIG_ROUTES.has(name);
}

/**
 * 没有模型时该把用户送到哪个配置页。
 *
 * 平台管理员配平台默认模型能让所有人都能用，优先送他去那里；其他人只能绑自己的。
 *
 * @param {{isPlatformAdmin?: boolean}} session
 * @returns {string}
 */
export function modelSetupRoute(session) {
  return session?.isPlatformAdmin ? "ops" : "account";
}
