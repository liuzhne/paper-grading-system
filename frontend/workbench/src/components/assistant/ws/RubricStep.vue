<script setup>
/**
 * 评分标准（用已发布标准直接评分时的第 1 步）：用哪一份、有哪些评分项。
 * 只在等选文件时能换一份——上传后任务已经锁定了评分标准（v2 界面改造方案 §4.3）。
 */
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";

import { api, StaleContextError } from "@/api/client.js";
import { publishedRubrics } from "@/lib/assistant-flow.js";
import { documentTypeLabel } from "@/lib/document-types.js";
import { useAssistantStore } from "@/stores/assistant.js";

import WsLayout from "./WsLayout.vue";

const props = defineProps({
  context: { type: Object, required: true },
  head: { type: Object, required: true },
});

const router = useRouter();
const store = useAssistantStore();
const rubric = ref(null);
const choices = ref(null);
const chosen = ref("");
const loadError = ref(null);

const rubricId = computed(() => props.context.rubricId || null);
const canChange = computed(() => store.pending?.kind === "pick_files");
const total = computed(() => (rubric.value?.criteria || []).reduce((sum, item) => sum + Number(item.max_score || 0), 0));

async function load() {
  if (!rubricId.value) return;
  try {
    rubric.value = await api.get(`/rubrics/${rubricId.value}`);
  } catch (err) {
    if (!(err instanceof StaleContextError)) loadError.value = "无法加载评分标准";
  }
}
async function loadChoices() {
  try {
    choices.value = publishedRubrics((await api.get("/rubrics")) || []);
  } catch (err) {
    if (!(err instanceof StaleContextError)) loadError.value = "无法加载评分标准列表";
  }
}
onMounted(load);
watch(rubricId, load);

async function change() {
  if (!chosen.value || chosen.value === rubricId.value) return;
  await store.changeRubric(chosen.value);
  choices.value = null;
  chosen.value = "";
}
</script>

<template>
  <WsLayout v-bind="head" :link="rubricId ? { href: router.resolve(`/rubrics?rubric=${rubricId}`).href, label: '在评分标准页打开' } : null">
    <div class="body">
      <div v-if="rubric" class="summary">
        <div class="row-main">
          <div class="name">《{{ rubric.name }}》<span class="mono faint">{{ rubric.version }}</span></div>
          <div class="row-sub">{{ documentTypeLabel(rubric.business_profile_key) }} · {{ rubric.criteria?.length || 0 }} 个评分项 · 合计 {{ total }} 分</div>
        </div>
        <span class="tag tag-accent">已发布</span>
      </div>

      <div v-if="canChange" class="change">
        <button v-if="!choices" class="text-btn" type="button" @click="loadChoices">换一份评分标准</button>
        <template v-else>
          <select v-model="chosen" class="select" aria-label="换成哪份评分标准">
            <option value="">选择已发布的评分标准</option>
            <option v-for="item in choices" :key="item.id" :value="item.id" :disabled="item.id === rubricId">
              {{ item.name }} {{ item.version }} · {{ documentTypeLabel(item.business_profile_key) }}
            </option>
          </select>
          <button class="btn btn-sm" type="button" :disabled="!chosen || store.sending" @click="change">换成这份</button>
          <button class="text-btn is-muted" type="button" @click="choices = null">取消</button>
        </template>
      </div>

      <template v-if="rubric?.criteria?.length">
        <div class="sec-head"><strong>评分项</strong><span>{{ rubric.criteria.length }} 项 · 合计 {{ total }} 分</span></div>
        <div class="row-list">
          <div v-for="item in rubric.criteria" :key="item.id" class="row">
            <span class="row-idx">{{ item.code }}</span>
            <span class="row-main">{{ item.name }}</span>
            <span class="mono max">{{ Number(item.max_score) }} 分</span>
          </div>
        </div>
      </template>
      <p v-if="loadError" class="notice notice-danger">{{ loadError }}</p>
    </div>
    <template #foot>
      <p class="foot-text">{{ canChange ? "选文件之前可以换一份；上传后任务就锁定这份评分标准。" : "本次评分按这一份执行。" }}</p>
    </template>
  </WsLayout>
</template>

<style scoped>
.body { padding: 14px 24px 0; }
.summary { display: flex; gap: 12px; align-items: center; padding: 12px 14px; border: 1px solid var(--border-light); border-radius: var(--radius-md); }
.name { font-size: 14px; font-weight: 600; }
.change { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
.change .select { flex: 1 1 220px; height: 32px; }
.max { flex: none; font-size: 12.5px; color: var(--text-secondary); }
</style>
