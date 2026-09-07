<script setup>
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { api, ApiError } from "@/api/client.js";
import { useSessionStore } from "@/stores/session.js";

const session = useSessionStore();
const route = useRoute();
const router = useRouter();

const username = ref("");
const password = ref("");
const submitting = ref(false);
const error = ref(null);

async function onSubmit() {
  submitting.value = true;
  error.value = null;
  try {
    await api.post("/auth/login", {
      username: username.value.trim(),
      password: password.value,
    });
    await session.bootstrap();
    const redirect = route.query.redirect;
    await router.push(typeof redirect === "string" ? redirect : { name: "dashboard" });
  } catch (err) {
    error.value =
      err instanceof ApiError && err.status === 401
        ? "用户名或密码错误"
        : err?.message || "登录失败，请稍后重试";
  } finally {
    submitting.value = false;
    password.value = "";
  }
}
</script>

<template>
  <div class="auth">
    <section class="pitch">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">
          <svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="#fff"
               stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 8.6V4h4.6" />
            <path d="M20 15.4V20h-4.6" />
            <path d="M8.2 12.4l3 3 5.2-5.8" />
          </svg>
        </span>
        <span class="brand-name">有据智评</span>
      </div>

      <div class="pitch-body">
        <p class="eyebrow mono">GRADING &amp; REVIEW WORKBENCH</p>
        <h1>每一个分数<br />都能回到原文那一段</h1>
        <p class="lede">
          面向课程作业与学位论文的批量评审系统。评分标准以已发布版本固化，评分结果逐项标注证据出处，人工复核留痕后统一导出。
        </p>
        <ol class="points">
          <li><span class="mono">01</span><span>评分标准经审核发布后不可变，全批次口径一致</span></li>
          <li><span class="mono">02</span><span>每项给分附页码与章节引用，可点击回到原文</span></li>
          <li><span class="mono">03</span><span>改分写入复核记录，报告与成绩单一次导出</span></li>
        </ol>
      </div>

      <p class="foot mono">评审工作台</p>
    </section>

    <section class="form-side">
      <div class="form-wrap">
        <h2>登录</h2>
        <p class="sub">使用院系分配的组织账户进入工作台。</p>

        <form @submit.prevent="onSubmit">
          <label for="login-username">用户名</label>
          <input
            id="login-username"
            v-model="username"
            type="text"
            autocomplete="username"
            required
          />

          <div class="pw-row">
            <label for="login-password">密码</label>
            <a href="/reset-password">重置密码</a>
          </div>
          <input
            id="login-password"
            v-model="password"
            type="password"
            autocomplete="current-password"
            required
          />

          <button type="submit" :disabled="submitting">
            {{ submitting ? "登录中…" : "登录" }}
          </button>

          <p v-if="error" class="error" role="alert">{{ error }}</p>
        </form>

        <div class="invite">
          <p>系统不开放自助注册。首次使用请通过管理员发送的邀请链接完成账户设置。</p>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.auth {
  display: grid;
  grid-template-columns: 1.08fr 1fr;
  min-height: 100vh;
}

.pitch {
  background: var(--sidebar-bg);
  color: #fff;
  padding: 54px 62px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

.brand-mark {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: var(--accent);
  display: grid;
  place-items: center;
}

.brand-name {
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0.16em;
}

.pitch-body {
  max-width: 470px;
}

.eyebrow {
  font-size: 11.5px;
  letter-spacing: 0.2em;
  color: var(--sidebar-text-faint);
  margin: 0 0 22px;
}

h1 {
  margin: 0 0 20px;
  font-size: 39px;
  line-height: 1.28;
  font-weight: 700;
  text-wrap: pretty;
}

.lede {
  margin: 0 0 38px;
  font-size: 15px;
  line-height: 1.85;
  color: #a8b0ac;
  text-wrap: pretty;
}

.points {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 13px;
}

.points li {
  display: flex;
  gap: 13px;
  align-items: baseline;
  font-size: 14px;
  color: #cdd3d0;
}

.points .mono {
  font-size: 11.5px;
  color: #6fa196;
}

.foot {
  font-size: 11.5px;
  color: #6b736f;
  margin: 0;
}

.form-side {
  background: var(--surface);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px;
}

.form-wrap {
  width: 100%;
  max-width: 368px;
}

h2 {
  margin: 0 0 8px;
  font-size: 25px;
  font-weight: 700;
}

.sub {
  margin: 0 0 34px;
  font-size: 14px;
  color: var(--text-muted);
}

label {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 8px;
}

input {
  width: 100%;
  height: 44px;
  padding: 0 14px;
  border: 1px solid var(--border-input);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--text);
  margin-bottom: 20px;
}

.pw-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}

.pw-row label {
  margin-bottom: 8px;
}

.pw-row a {
  font-size: 12px;
  color: var(--text-muted);
}

button {
  width: 100%;
  height: 46px;
  border: 0;
  border-radius: var(--radius);
  background: var(--accent);
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
}

button:hover:not(:disabled) {
  filter: brightness(1.12);
}

button:disabled {
  opacity: 0.6;
  cursor: default;
}

.error {
  margin: 14px 0 0;
  font-size: 13px;
  color: var(--danger);
}

.invite {
  margin-top: 30px;
  padding-top: 22px;
  border-top: 1px solid var(--border-light);
}

.invite p {
  margin: 0;
  font-size: 13px;
  color: var(--text-muted);
  line-height: 1.75;
}

@media (max-width: 900px) {
  .auth {
    grid-template-columns: 1fr;
  }

  .pitch {
    padding: 32px 24px;
  }

  .form-side {
    padding: 32px 24px;
  }
}
</style>
