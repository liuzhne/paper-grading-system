// @ts-check
import { ref } from "vue";

/**
 * 模型可用性：判定、拦截、留痕（D-027、D-028、D-032、D-039）。
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

/** 配置页里该定位到哪个区块。跳到页顶不够——那两页都还有别的内容。 */
/** @type {Record<string, string>} */
export const SETUP_ANCHOR = {
  account: "ai-connections",
  ops: "platform-llm",
};

/** 各功能页的中文名，用来告诉用户「你刚才想去的是哪一页」。 */
/** @type {Record<string, string>} */
const ROUTE_LABEL = {
  dashboard: "工作台",
  tasks: "评分任务",
  "task-new": "新建评分任务",
  grade: "评分工作区",
  review: "结果复核",
  rubrics: "评分标准",
  exports: "输出中心",
};

/**
 * 最近一次**被拦下的导航**。`null` 表示没有待解释的拦截。
 *
 * 这不是「弹过没有」的开关——是「这一次点击被挡了」的事实。两者的区别正是这次
 * 缺陷的全部：上一版用组件级的 `dismissed`，关掉一次整个会话不再提示，用户点任何
 * 入口都变成**被静默弹回配置页**，屏幕上没有任何解释。
 *
 * 每次守卫拦截都写入一条新的；关闭弹窗只清掉当前这一条。**没有任何标记跨越两次
 * 导航**，所以判据始终是实时的 `can_use_llm`，而不是历史行为。
 *
 * @type {import("vue").Ref<{name: string, label: string, fullPath: string, at: number}|null>}
 */
export const pendingBlock = ref(null);

/**
 * 记下这一次被拦下的导航。
 *
 * @param {{name?: string|symbol|null, fullPath?: string}} to
 */
export function noteBlockedAttempt(to) {
  const name = typeof to?.name === "string" ? to.name : "";
  // 每次都建新对象：同一个目标连点两次也是两次拦截，弹窗要重新出现。
  pendingBlock.value = {
    name,
    label: ROUTE_LABEL[name] || "该页面",
    fullPath: to?.fullPath || "",
    at: Date.now(),
  };
}

/** 用户看过这一条了。下一次被拦会产生新的一条。 */
export function clearBlockedAttempt() {
  pendingBlock.value = null;
}
