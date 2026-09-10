// @ts-check
import { computed, onMounted, watch } from "vue";
import { useRoute } from "vue-router";

/**
 * 让配置区块能被 URL 锚点定位并短暂高亮（D-039）。
 *
 * 未配置模型的守卫会把用户送到 `account#ai-connections` 或 `ops#platform-llm`。
 * 只跳到页顶不够：这两页都还有别的区块（当前身份、组织成员、门禁信号……），
 * 用户被送过来还得自己找「到底要我配哪个」。
 *
 * 浏览器的原生锚点滚动在 SPA 里不可靠——目标区块往往在数据回来之后才渲染，那时
 * 浏览器早就处理完这一帧了。所以自己滚。
 *
 * @param {string} id 区块的 DOM id
 * @returns {{highlighted: import("vue").ComputedRef<boolean>}}
 */
export function useAnchorHighlight(id) {
  const route = useRoute();
  const highlighted = computed(() => route.hash === `#${id}`);

  function reveal() {
    if (!highlighted.value) return;
    // 等一帧：区块可能在同一个 tick 里才刚挂上。
    requestAnimationFrame(() => {
      document.getElementById(id)?.scrollIntoView({ block: "start" });
    });
  }

  onMounted(reveal);
  watch(highlighted, reveal);

  return { highlighted };
}
