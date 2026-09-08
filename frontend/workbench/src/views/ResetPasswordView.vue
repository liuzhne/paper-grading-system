<script setup>
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { api, ApiError } from "@/api/client.js";
import AuthLayout from "@/layouts/AuthLayout.vue";

/**
 * 密码重置（旧壳下线后由工作台承接）。
 *
 * 与邀请注册同理：邮件里发出去的是 `/reset-password?token=...`，这一页不接就
 * 等于把所有在途重置链接作废。
 *
 * 两次密码一致与长度都在提交前当场校验：后端也会校验，但让用户填完一整个表单
 * 再被打回，只是把一个本可以立刻说清的问题拖到一次往返之后。
 */
const route = useRoute();
const router = useRouter();

const token = ref("");
const password = ref("");
const confirmation = ref("");
const submitting = ref(false);
const error = ref(null);
const done = ref(false);

onMounted(() => {
  const raw = route.query.token;
  token.value = typeof raw === "string" ? raw : "";
});

async function onSubmit() {
  error.value = null;
  if (!token.value) {
    error.value = "重置链接无效或已过期";
    return;
  }
  if (password.value !== confirmation.value) {
    error.value = "两次输入的密码不一致";
    return;
  }
  if (password.value.length < 12) {
    error.value = "密码至少需要 12 个字符";
    return;
  }

  submitting.value = true;
  try {
    await api.post("/auth/password-reset/confirm", {
      token: token.value,
      password: password.value,
      password_confirmation: confirmation.value,
    });
    done.value = true;
    await router.push({ name: "login", query: { reset: "1" } });
  } catch (err) {
    error.value =
      err instanceof ApiError && err.status === 400
        ? "重置链接无效或已过期"
        : err?.message || "重置失败，请稍后重试";
  } finally {
    submitting.value = false;
    password.value = "";
    confirmation.value = "";
  }
}
</script>

<template>
  <AuthLayout heading="重置密码" sub="设置一个新密码后即可重新登录。">
    <p v-if="done" class="sub">密码已更新，正在返回登录…</p>

    <form v-else @submit.prevent="onSubmit">
      <label for="reset-password">新密码</label>
      <input
        id="reset-password"
        v-model="password"
        type="password"
        autocomplete="new-password"
        required
      />
      <p class="sub">至少 12 个字符。</p>

      <label for="reset-confirmation">确认新密码</label>
      <input
        id="reset-confirmation"
        v-model="confirmation"
        type="password"
        autocomplete="new-password"
        required
      />

      <button type="submit" :disabled="submitting">
        {{ submitting ? "提交中…" : "重置密码" }}
      </button>

      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </form>
  </AuthLayout>
</template>
