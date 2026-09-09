<script setup>
import { computed } from "vue";
import { RouterLink } from "vue-router";

import { useSessionStore } from "@/stores/session.js";

/**
 * 未配置可用模型时的阻断式弹窗（用户决定，2026-09-09；D-027、D-028）。
 *
 * 原先是「先跳到账户页，再显示一条横幅」。横幅容易被忽略，而且用户已经被送到一个
 * 自己没主动去的页面，不知道发生了什么。改为先说清楚，再由用户点进配置页。
 *
 * **不可关闭**：没有可用模型时整套评分能力都用不了，给一个「关闭」等于放人进去撞
 * 一连串失败。唯一的出路是去配置。
 */
const session = useSessionStore();

const llm = computed(() => session.capabilities?.llm ?? null);

// 能力表未加载时不弹：首屏还没算完就弹，正常用户会先看到一次误报。
const visible = computed(() => Boolean(llm.value) && !llm.value.can_use_llm);
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
      <h2 id="model-setup-title" class="dialog-title">尚未配置可用模型</h2>
      <p id="model-setup-desc" class="dialog-body">
        本部署还没有可用的评分模型，现在无法开始评分。
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
          :to="{ name: 'ops' }"
        >
          去「运维与质量」配置平台模型
        </RouterLink>
        <RouterLink
          class="btn"
          :class="{ 'btn-primary': !session.isPlatformAdmin }"
          :to="{ name: 'account' }"
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
  width: 100%;
  max-width: 460px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  box-shadow: 0 18px 48px rgba(26, 28, 25, 0.18);
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
