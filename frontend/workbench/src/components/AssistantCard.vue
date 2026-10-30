<script setup>
/**
 * 留在对话里的卡片：对话本身产生的选择与确认（选任务、选材料、重试、取消、要不要放弃上一步、失败诊断）、
 * 快捷按钮与页面链接，以及旧流程的“是否移除解析失败的文件”。
 * 流程卡片与结果卡片显示成“在右侧查看”入口（`assistant/EntryCard.vue`），交互在工作区里（v2 界面改造方案 §3.3）。
 *
 * 按钮不直接调业务接口：等待中的卡片通过 `resume` 通知后端的流程图继续，选择卡片通过 `select` 发起一次查询或确认；
 * 写动作由后端经共享守卫执行（方案 T4、T14）。
 */
import { computed } from "vue";
import { useRouter } from "vue-router";

import { useAssistantStore } from "@/stores/assistant.js";

const props = defineProps({
  message: { type: Object, required: true },
  card: { type: Object, required: true },
});

const store = useAssistantStore();
const router = useRouter();

const waiting = computed(() => store.isPending(props.card));
const proposed = computed(() => props.card.status === "proposed");
const disabled = computed(() => store.sending || store.uploading);

const STATUS_TEXT = { running: "进行中…", done: "已完成", failed: "未完成", dismissed: "已放弃", expired: "已过期" };
const statusText = computed(() => STATUS_TEXT[props.card.status] || "");
// 打断确认卡片：done 是放弃了上一步，dismissed 是选了继续上一步。
const SWITCH_STATUS_TEXT = { done: "已放弃上一步", dismissed: "已继续上一步", expired: "已过期" };
// 选择卡片选完后写明选了哪一个，不只是“已完成”。
const chosenText = computed(() => {
  const result = props.card.result || {};
  const picked = props.card.type === "choose_batch"
    ? (props.card.batches || []).find((item) => item.id === result.batch_id)?.name
    : (props.card.papers || []).find((item) => item.paper_id === result.paper_id)?.label;
  return picked ? `已选：${picked}` : statusText.value;
});
// 完整页面在新标签页打开：选好的待评分文件只在本页内存里，跳走就丢了。
const linkHref = computed(() => (props.card.path ? router.resolve(props.card.path).href : "#"));
</script>

<template>
  <div class="acard" :class="[`acard-${card.type}`, `is-${card.status || 'info'}`]" :data-card-id="card.id">
    <!-- 快捷按钮 -->
    <div v-if="card.type === 'quick_actions'" class="chips">
      <button v-for="action in card.actions" :key="action.label" class="chip-q" type="button"
              :disabled="disabled" @click="store.send(action.text, action.intent)">{{ action.label }}</button>
    </div>

    <!-- 页面链接 -->
    <a v-else-if="card.type === 'link'" class="btn btn-sm" :href="linkHref" target="_blank" rel="noopener">{{ card.label }} →</a>

    <!-- 旧流程：无法解析的文件，移除并继续 -->
    <template v-else-if="card.type === 'blocked_files'">
      <div v-if="waiting && proposed" class="btn-row">
        <button class="btn btn-primary btn-sm" type="button" :disabled="disabled" @click="store.resume(card.id, { action: 'remove' })">移除并评其余文件</button>
        <button class="btn btn-sm" type="button" :disabled="disabled" @click="store.resume(card.id, { action: 'cancel' })">先不评</button>
      </div>
      <p v-else class="faint small">{{ statusText }}</p>
    </template>

    <!-- 失败诊断 -->
    <template v-else-if="card.type === 'diagnosis'">
      <ul class="groups">
        <li v-for="group in card.groups" :key="group.code">
          <div class="group-head"><strong>{{ group.label }}</strong> <span class="mono faint">{{ group.count }} 份 · {{ group.code }}</span></div>
          <p class="advice">{{ group.advice }}</p>
          <p class="faint small">{{ group.file_names.join("、") }}<template v-if="group.count > group.file_names.length"> 等</template></p>
        </li>
      </ul>
      <div class="btn-row">
        <button v-if="proposed" class="btn btn-primary btn-sm" type="button" :disabled="disabled"
                @click="store.select(card.id, { action: 'retry' })">重试失败项</button>
        <a class="btn btn-sm" :href="router.resolve('/account').href" target="_blank" rel="noopener">账户与连接</a>
      </div>
    </template>

    <!-- 选择任务 / 选择材料 -->
    <template v-else-if="card.type === 'choose_batch' || card.type === 'choose_paper'">
      <div v-if="proposed" class="choices">
        <template v-if="card.type === 'choose_batch'">
          <button v-for="batch in card.batches" :key="batch.id" class="choice" type="button" :disabled="disabled"
                  @click="store.select(card.id, { batch_id: batch.id })">{{ batch.name }}</button>
        </template>
        <template v-else>
          <button v-for="paper in card.papers" :key="paper.paper_id" class="choice" type="button" :disabled="disabled"
                  @click="store.select(card.id, { paper_id: paper.paper_id })">{{ paper.label }}</button>
        </template>
      </div>
      <p v-else class="faint small">{{ chosenText }}</p>
    </template>

    <!-- 重试 / 取消 -->
    <template v-else-if="card.type === 'confirm_retry' || card.type === 'confirm_cancel'">
      <div v-if="proposed" class="btn-row">
        <button class="btn btn-sm" :class="card.type === 'confirm_cancel' ? 'btn-danger' : 'btn-primary'" type="button" :disabled="disabled"
                @click="store.select(card.id, { action: 'confirm' })">
          {{ card.type === "confirm_cancel" ? "取消剩余评分" : "重试" }}
        </button>
        <button class="btn btn-sm" type="button" :disabled="disabled" @click="store.select(card.id, { action: 'cancel' })">算了</button>
      </div>
      <p v-else class="faint small">{{ statusText }}</p>
    </template>

    <!-- 打断保护：上一步还在等老师回应时，先问要不要放弃 -->
    <template v-else-if="card.type === 'confirm_switch_flow'">
      <div v-if="proposed" class="btn-row">
        <button class="btn btn-sm btn-primary" type="button" :disabled="disabled"
                @click="store.switchFlow(card, 'switch')">放弃并{{ card.to_label }}</button>
        <button class="btn btn-sm" type="button" :disabled="disabled"
                @click="store.switchFlow(card, 'keep')">继续“{{ card.from_label }}”</button>
      </div>
      <p v-else class="faint small">{{ SWITCH_STATUS_TEXT[card.status] || statusText }}</p>
    </template>
  </div>
</template>

<style scoped>
.acard { margin-top: 12px; max-width: 520px; padding: 12px 14px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); }
.acard-quick_actions, .acard-link { padding: 0; border: 0; background: none; }
.is-dismissed, .is-expired { opacity: 0.6; }
.chips, .choices { display: flex; flex-wrap: wrap; gap: 6px; }
.chip-q { height: 28px; padding: 0 11px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface);
  color: var(--text-secondary); font: inherit; font-size: 12.5px; cursor: pointer; }
.chip-q:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.choice { padding: 7px 11px; border: 1px solid var(--border-input); border-radius: 8px; background: var(--surface); cursor: pointer;
  text-align: left; font: inherit; font-size: 13px; }
.choice:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.choice:disabled, .chip-q:disabled { opacity: 0.55; cursor: default; }
.small { margin: 0; font-size: 12px; }
.groups { margin: 0 0 10px; padding: 0; list-style: none; }
.groups li { padding: 8px 0; border-bottom: 1px solid var(--border-row); }
.group-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.advice { margin: 4px 0; font-size: 13px; line-height: 1.7; }
</style>
