// @ts-check
/**
 * 是否需要先去配置模型（D-027、D-028）。
 *
 * 平台未配置默认模型、用户也没绑自己的 API key 连接时，整套评分能力都用不了。
 *
 * **判定与呈现分开**：这里只回答「需不需要配置」，怎么告诉用户由
 * `ModelSetupDialog` 决定。此前是直接路由跳转——用户被送到一个自己没主动去的
 * 页面，还得猜发生了什么。
 *
 * @param {{capabilities: any}} session 会话 store
 * @returns {boolean}
 */
export function requiresModelSetup(session) {
  const llm = session?.capabilities?.llm;
  // 能力表还没加载完就判定，会让正常用户先看到一次误报。首屏未知时一律放行，
  // 加载完成后弹窗自然会出现。
  if (!llm) return false;
  return !llm.can_use_llm;
}
