<script setup>
import { onBeforeUnmount, onMounted, ref } from "vue";
import { RouterView, useRouter } from "vue-router";

/**
 * 首次导航要等会话接口（`/auth/me`、组织、能力表）返回才能决定去哪一页，
 * 这期间 `RouterView` 什么都不渲染。服务端冷启动时要十几秒，整页空白读起来
 * 就是“系统坏了”，所以先给一个加载提示，等得久了再说明原因。
 */
const SLOW_AFTER_MS = 4000;

const router = useRouter();
const ready = ref(false);
const slow = ref(false);
let timer = null;

onMounted(async () => {
  timer = window.setTimeout(() => { slow.value = true; }, SLOW_AFTER_MS);
  try {
    await router.isReady();
  } catch {
    // 首次导航失败时页面自己会显示错误，这里只负责撤掉加载提示。
  } finally {
    window.clearTimeout(timer);
    ready.value = true;
  }
});

onBeforeUnmount(() => window.clearTimeout(timer));
</script>

<template>
  <RouterView />
  <div v-if="!ready" class="boot" role="status" aria-live="polite" data-test="app-boot">
    <p class="boot-title">正在连接服务器…</p>
    <p v-if="slow" class="boot-note">服务器空闲一段时间后需要重新启动，首次打开可能要十几秒，请稍候。</p>
  </div>
</template>

<style scoped>
.boot { display: grid; place-content: center; gap: 8px; min-height: 100vh; padding: 16px; text-align: center; background: var(--bg); color: var(--text-secondary); }
.boot-title { margin: 0; font-size: 14px; }
.boot-note { margin: 0; max-width: 360px; font-size: 12px; color: var(--text-muted); line-height: 1.6; }
</style>
