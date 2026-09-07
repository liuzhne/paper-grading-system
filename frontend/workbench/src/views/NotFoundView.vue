<script setup>
import { RouterLink } from "vue-router";
</script>

<template>
  <div class="wrap">
    <p class="code mono">404</p>
    <h1>页面不存在</h1>
    <p class="hint">
      该地址不属于评审工作台。返回<RouterLink :to="{ name: 'dashboard' }">工作台</RouterLink>，
      或前往<a href="/">旧版工作台</a>。
    </p>
  </div>
</template>

<style scoped>
.wrap {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 32px;
  text-align: center;
}

.code {
  margin: 0;
  font-size: 13px;
  letter-spacing: 0.2em;
  color: var(--text-faint);
}

h1 {
  margin: 0;
  font-size: 24px;
  font-weight: 700;
}

.hint {
  margin: 6px 0 0;
  font-size: 14px;
  color: var(--text-muted);
}
</style>
