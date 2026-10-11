<script setup>
/**
 * 第一次使用评分助手时选择助手模型（维护者决定 U9）。
 * 不选就按默认顺序：当前启用的个人连接 → 平台模型（较慢）。
 */
import { computed, ref, watch } from "vue";

import { ApiError } from "@/api/client.js";
import { useAssistantStore } from "@/stores/assistant.js";

const store = useAssistantStore();
const choice = ref("");
const saving = ref(false);
const error = ref(null);

const settings = computed(() => store.settings);
const options = computed(() => {
  const list = (settings.value?.connections || []).map((item) => ({
    value: `connection:${item.id}`, label: `${item.name} · ${item.model_name}`, hint: "自己的模型连接",
  }));
  if (settings.value?.platform_available) {
    list.push({ value: "platform", label: "平台模型", hint: "所有用户共用，响应会非常慢" });
  }
  return list;
});

watch(() => store.setupOpen, (open) => {
  if (!open) return;
  error.value = null;
  const current = settings.value;
  if (current?.model_source === "platform") choice.value = "platform";
  else if (current?.ai_connection_id) choice.value = `connection:${current.ai_connection_id}`;
  else choice.value = options.value[0]?.value || "";
}, { immediate: true });

async function save() {
  if (!choice.value) return;
  saving.value = true;
  error.value = null;
  try {
    const [source, id] = choice.value.split(":");
    await store.saveSettings(source === "platform"
      ? { model_source: "platform" }
      : { model_source: "connection", ai_connection_id: id });
  } catch (err) {
    error.value = err instanceof ApiError && typeof err.detail === "string" ? err.detail : "保存失败";
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div v-if="store.setupOpen" class="backdrop" role="dialog" aria-modal="true" aria-labelledby="assistant-setup-title">
    <div class="dialog card card-pad">
      <h2 id="assistant-setup-title" class="card-title">选择助手使用的模型</h2>
      <p class="faint">助手只用模型理解你的自由输入；评分本身仍用评分任务绑定的模型。</p>

      <div v-if="options.length" class="options">
        <label v-for="option in options" :key="option.value" class="option" :class="{ selected: choice === option.value }">
          <input v-model="choice" type="radio" name="assistant-model" :value="option.value" />
          <span><span class="option-label">{{ option.label }}</span><span class="faint option-hint">{{ option.hint }}</span></span>
        </label>
      </div>
      <p v-else class="notice notice-warn">还没有可用的模型。可以先在“账户与连接”里添加自己的模型连接；没有模型时，助手仍能识别常见的说法。</p>

      <p v-if="choice === 'platform'" class="notice notice-warn">平台模型由所有用户共用，响应会非常慢。</p>
      <p v-if="error" class="notice notice-danger" role="alert">{{ error }}</p>

      <div class="btn-row actions">
        <button class="btn" type="button" @click="store.dismissSetup()">先用默认</button>
        <button class="btn btn-primary" type="button" :disabled="!choice || saving" @click="save">保存</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.backdrop { position: fixed; inset: 0; z-index: 40; display: grid; place-items: center; padding: 16px; background: rgba(26, 27, 25, 0.35); }
.dialog { width: min(480px, 100%); }
.options { display: grid; gap: 8px; margin: 14px 0; }
.option { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius); cursor: pointer; }
.option.selected { border-color: var(--accent); background: var(--accent-selected); }
.option-label { display: block; }
.option-hint { display: block; font-size: 12px; }
.actions { justify-content: flex-end; margin-top: 14px; }
</style>
