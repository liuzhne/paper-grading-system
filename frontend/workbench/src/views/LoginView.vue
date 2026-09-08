<script setup>
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { api, ApiError } from "@/api/client.js";
import { useSessionStore } from "@/stores/session.js";
import AuthLayout from "@/layouts/AuthLayout.vue";

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
  <AuthLayout heading="登录" sub="使用院系分配的组织账户进入工作台。">
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
  </AuthLayout>
</template>
