<script setup>
/**
 * 未交付页面的占位视图（计划 §8.3 新旧并存）。
 * 明确标注所属阶段，并提供回旧 SPA 的入口；不携带原文或凭据。
 */
defineProps({
  title: { type: String, required: true },
  stage: { type: String, required: true },
  summary: { type: String, default: "" },
  scope: { type: Array, default: () => [] },
  legacyHref: { type: String, default: "/" },
});
</script>

<template>
  <div>
    <header class="head">
      <p class="eyebrow mono">阶段 {{ stage }} · 尚未交付</p>
      <h1>{{ title }}</h1>
      <p v-if="summary" class="summary">{{ summary }}</p>
    </header>

    <section class="card">
      <h2>本页计划范围</h2>
      <ul v-if="scope.length">
        <li v-for="item in scope" :key="item">{{ item }}</li>
      </ul>
      <p v-else class="muted">范围见改造计划对应章节。</p>

      <p class="fallback">
        该页面交付前请继续使用
        <a :href="legacyHref">旧版工作台</a>；旧页在并存期仍受新服务端守卫约束。
      </p>
    </section>
  </div>
</template>

<style scoped>
.head {
  margin-bottom: 24px;
}

.eyebrow {
  font-size: 11.5px;
  letter-spacing: 0.14em;
  color: var(--text-faint);
  margin: 0 0 8px;
}

h1 {
  margin: 0 0 6px;
  font-size: 27px;
  font-weight: 700;
}

.summary {
  margin: 0;
  font-size: 14px;
  color: var(--text-muted);
}

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 22px;
}

h2 {
  margin: 0 0 12px;
  font-size: 15px;
  font-weight: 650;
}

ul {
  margin: 0;
  padding-left: 20px;
  color: var(--text-secondary);
  font-size: 13.5px;
  line-height: 1.9;
}

.muted {
  color: var(--text-faint);
  font-size: 13.5px;
  margin: 0;
}

.fallback {
  margin: 20px 0 0;
  padding-top: 16px;
  border-top: 1px solid var(--border-light);
  font-size: 13px;
  color: var(--text-muted);
}
</style>
