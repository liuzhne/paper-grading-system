<script setup>
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { api, ApiError } from "@/api/client.js";
import AuthLayout from "@/layouts/AuthLayout.vue";

/**
 * 邀请注册（旧壳下线后由工作台承接）。
 *
 * 邮件里已经发出去的链接形如 `/register?token=...`，收件人不会重新拿到新链接。
 * 这一页必须继续认这个 token，否则所有在途邀请一起作废，而收件人只会看到 404。
 *
 * 后端不开放自助注册：没有 token 或 token 失效时直接说清楚，不摆一个提交了也
 * 只会拿到 403 的表单。
 */
const route = useRoute();
const router = useRouter();

const token = ref("");
const invitation = ref(null);
const resolving = ref(true);
const resolveError = ref(null);

const username = ref("");
const displayName = ref("");
const password = ref("");
const submitting = ref(false);
const error = ref(null);

onMounted(async () => {
  const raw = route.query.token;
  token.value = typeof raw === "string" ? raw : "";
  if (!token.value) {
    resolving.value = false;
    resolveError.value = "邀请链接无效或已过期";
    return;
  }
  try {
    invitation.value = await api.post("/auth/invitations/resolve", {
      token: token.value,
    });
  } catch (err) {
    resolveError.value =
      err instanceof ApiError && err.status === 400
        ? "邀请链接无效或已过期"
        : err?.message || "无法读取邀请信息";
  } finally {
    resolving.value = false;
  }
});

async function onSubmit() {
  submitting.value = true;
  error.value = null;
  try {
    await api.post("/auth/register", {
      username: username.value.trim(),
      // 邮箱来自邀请本身，不让用户改：后端要求两者一致，让它可编辑只会制造
      // 一个必然失败的输入框。
      email: invitation.value.email,
      display_name: displayName.value.trim(),
      password: password.value,
      invitation_token: token.value,
    });
    await router.push({ name: "login", query: { registered: "1" } });
  } catch (err) {
    error.value = err?.message || "注册失败，请稍后重试";
  } finally {
    submitting.value = false;
    password.value = "";
  }
}
</script>

<template>
  <AuthLayout heading="接受邀请" sub="通过管理员发送的邀请链接完成账户设置。">
    <p v-if="resolving" class="sub">正在读取邀请信息…</p>

    <p v-else-if="resolveError" class="error" role="alert">
      {{ resolveError }}。请联系管理员重新发送邀请。
    </p>

    <form v-else @submit.prevent="onSubmit">
      <p class="sub">
        <b>{{ invitation.organization_name }}</b> 邀请
        <span class="mono">{{ invitation.email }}</span>
        以 {{ invitation.role }} 身份加入。
      </p>

      <label for="reg-username">用户名</label>
      <input
        id="reg-username"
        v-model="username"
        type="text"
        autocomplete="username"
        required
      />

      <label for="reg-display">姓名</label>
      <input
        id="reg-display"
        v-model="displayName"
        type="text"
        autocomplete="name"
        required
      />

      <label for="reg-password">密码</label>
      <input
        id="reg-password"
        v-model="password"
        type="password"
        autocomplete="new-password"
        minlength="12"
        required
      />
      <p class="sub">至少 12 个字符。</p>

      <button type="submit" :disabled="submitting">
        {{ submitting ? "提交中…" : "完成注册" }}
      </button>

      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </form>
  </AuthLayout>
</template>
