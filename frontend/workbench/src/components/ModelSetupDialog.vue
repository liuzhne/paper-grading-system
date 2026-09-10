<script setup>
import { computed } from "vue";
import { RouterLink } from "vue-router";

import {
  SETUP_ANCHOR,
  clearBlockedAttempt,
  modelSetupRoute,
  pendingBlock,
  requiresModelSetup,
} from "@/router/llm-gate.js";
import { useSessionStore } from "@/stores/session.js";

/**
 * 未配置可用模型时的引导弹窗（D-027、D-028、D-032、D-039）。
 *
 * **显示条件是「刚刚有一次导航被拦下」，不是「本次会话弹过没有」。**
 *
 * 上一版用组件级的 `dismissed` ref 记「已经关过了」。组件挂在 `AppShell` 上跨路由
 * 存活，于是关掉一次整个会话不再提示——拦截还在，但用户点「评分任务」只会被静默
 * 弹回配置页，屏幕上没有任何解释。一次性标记正是这个缺陷的根源。
 *
 * 现在关闭只清掉**当前这一条**拦截记录。用户没配置就离开配置页、再点任何入口，
 * 守卫会写入新的一条，弹窗重新出现。判据自始至终是实时的 `can_use_llm`。
 *
 * 仍然可关闭：遮罩铺满视口，用户到了配置页还挡着就填不了表——那是 2026-09-10 的
 * 修复，保留。主动走到配置页（没被拦）不会有记录，因此不弹。
 */
const session = useSessionStore();

const needsSetup = computed(() => requiresModelSetup(session));

// 两个条件都要：配置成功后即便还留着一条旧记录也不再弹。
const visible = computed(() => needsSetup.value && Boolean(pendingBlock.value));

/** 用户刚才想去哪。只说「没有模型」，他还是不知道自己点的那一下发生了什么。 */
const attemptLabel = computed(() => pendingBlock.value?.label || "该页面");

const setupRoute = computed(() => modelSetupRoute(session));

/** 带锚点跳转：那两页都还有别的区块，落在页顶用户得自己找。 */
function setupTarget(name) {
  return { name, hash: `#${SETUP_ANCHOR[name]}` };
}

function dismiss() {
  clearBlockedAttempt();
}
</script>

<template>
  <div v-if="visible" class="dialog-mask" role="presentation">
    <div
      class="dialog"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="model-setup-title"
      aria-describedby="model-setup-desc"
    >
      <button
        class="dialog-close"
        type="button"
        data-test="close"
        aria-label="关闭"
        @click="dismiss"
      >
        ×
      </button>

      <h2 id="model-setup-title" class="dialog-title">请先配置 API Key</h2>
      <p id="model-setup-desc" class="dialog-body">
        本部署还没有可用的评分模型，<b>{{ attemptLabel }}</b> 暂时打不开。
        <template v-if="session.isPlatformAdmin">
          你可以配置平台默认模型让所有人都能用，或先绑定自己的 AI 连接。
        </template>
        <template v-else>
          请绑定自己的 AI 连接，或联系平台管理员配置平台默认模型。
        </template>
      </p>

      <p class="dialog-note">
        没有模型时给出明确失败，好过悄悄产出无效的分数。
      </p>

      <div class="dialog-actions">
        <RouterLink
          v-if="session.isPlatformAdmin"
          class="btn btn-primary"
          :to="setupTarget('ops')"
          @click="dismiss"
        >
          去「运维与质量」配置平台模型
        </RouterLink>
        <RouterLink
          class="btn"
          :class="{ 'btn-primary': !session.isPlatformAdmin }"
          :to="setupTarget('account')"
          @click="dismiss"
        >
          去「账户与连接」绑定 AI 连接
        </RouterLink>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dialog-mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(26, 28, 25, 0.42);
}

.dialog {
  position: relative;
  width: 100%;
  max-width: 460px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  box-shadow: 0 18px 48px rgba(26, 28, 25, 0.18);
}

.dialog-close {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
}

.dialog-close:hover {
  background: var(--surface-muted);
  color: var(--text-secondary);
}

.dialog-title {
  margin: 0 0 10px;
  font-size: 17px;
  font-weight: 700;
}

.dialog-body {
  margin: 0 0 12px;
  font-size: 13.5px;
  line-height: 1.8;
  color: var(--text-secondary);
}

.dialog-note {
  margin: 0 0 18px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-faint);
}

.dialog-actions {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 窄屏上按钮本来就是一列；宽一点时并排更省空间，但仍保持等宽。 */
@media (min-width: 520px) {
  .dialog-actions {
    flex-direction: row;
  }

  .dialog-actions > * {
    flex: 1;
    text-align: center;
  }
}
</style>
