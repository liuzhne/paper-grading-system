<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { useGradingStore } from "@/stores/grading.js";

const store = useGradingStore();
const route = useRoute();
const router = useRouter();

const position = computed(() =>
  store.neighbors ? `${store.neighbors.position} / ${store.neighbors.total}` : "—",
);

/** 定位状态决定这条证据能不能点、点了会发生什么。 */
const LOCATION_LABEL = {
  verified: "可定位",
  quote_not_found: "引文与当前文本不一致",
  unit_context: "证据上下文",
  anchor_missing: "锚点已失效",
  no_anchor: "无锚点",
  check_fact: "检查事实",
};

function locatable(view) {
  return ["verified", "quote_not_found", "unit_context"].includes(
    view.location_status,
  );
}

function evidenceChipText(view) {
  const parts = [];
  if (view.page_start) parts.push(`第 ${view.page_start} 页`);
  if (view.section_title) parts.push(view.section_title);
  return parts.join(" · ") || LOCATION_LABEL[view.location_status] || "证据";
}

async function onLocate(view) {
  if (!locatable(view)) return;
  store.locate({ anchor_id: view.anchor_id, quote: view.quote });
  if (view.anchor_id) await store.loadAnchorPage(view.anchor_id);
  const el = document.querySelector(`[data-anchor="${view.anchor_id}"]`);
  el?.scrollIntoView({ block: "center", behavior: "smooth" });
}

/**
 * 当前锚点命中该块。
 *
 * 必须先确认锚点非空：legacy 块的 evidence_unit_id 恒为 null，若直接比较
 * 就会在「未选中任何证据」时把整篇正文都标成高亮。
 */
function isAnchored(block) {
  const anchor = store.activeAnchorId;
  return Boolean(anchor) && (block.chunk_id === anchor || block.evidence_unit_id === anchor);
}

/** 高亮已验证的引文。未验证的引文不高亮——会指到错误位置。 */
function highlight(block) {
  const quote = store.activeQuote;
  const text = block.text || "";
  if (!quote || !text.includes(quote)) return [{ text, mark: false }];
  const at = text.indexOf(quote);
  return [
    { text: text.slice(0, at), mark: false },
    { text: quote, mark: true },
    { text: text.slice(at + quote.length), mark: false },
  ].filter((part) => part.text);
}

// --- 改分与确认（V3-5）-----------------------------------------------------
//
// 理由必填：改分会写进复核记录，没有理由的记录事后无法判断当初为什么改。
const editing = ref(null);
const draftScore = ref(null);
const draftReason = ref("");
const writeBusy = ref(false);
const writeError = ref(null);
const runNote = ref("");

function startEdit(item) {
  editing.value = item.id;
  draftScore.value = item.final_score ?? item.ai_score ?? 0;
  draftReason.value = "";
  writeError.value = null;
}

async function saveEdit(item) {
  writeBusy.value = true;
  writeError.value = null;
  try {
    await store.overrideScore(item.id, {
      score: Number(draftScore.value),
      reason: draftReason.value,
    });
    editing.value = null;
  } catch (err) {
    // 并发冲突要让用户看到并重新决定，不静默覆盖别人的改动。
    writeError.value = err instanceof Error ? err.message : "改分失败";
  } finally {
    writeBusy.value = false;
  }
}

async function confirmRun({ advance }) {
  writeBusy.value = true;
  writeError.value = null;
  try {
    await store.submitRunReview(store.currentRunId, runNote.value || "已逐项核对");
    runNote.value = "";
    if (advance) await goNeighbour("next");
  } catch (err) {
    writeError.value = err instanceof Error ? err.message : "提交复核失败";
  } finally {
    writeBusy.value = false;
  }
}

async function goNeighbour(direction) {
  const target =
    direction === "next"
      ? store.neighbors?.next_paper_id
      : store.neighbors?.previous_paper_id;
  if (target) await store.selectPaper(target);
}

function onKeydown(event) {
  if (event.target.matches("input, textarea, select")) return;
  if (event.key === "ArrowDown" || event.key === "j") goNeighbour("next");
  if (event.key === "ArrowUp" || event.key === "k") goNeighbour("previous");
}

onMounted(() => {
  store.openBatch(route.params.batchId);
  window.addEventListener("keydown", onKeydown);
});

onUnmounted(() => window.removeEventListener("keydown", onKeydown));
</script>

<template>
  <div class="workspace">
    <header class="topbar">
      <button class="back" type="button" @click="router.push({ name: 'tasks' })">←</button>
      <div class="ident">
        <div class="faint mono">{{ store.currentPaper?.student_id || "—" }}</div>
        <div class="title">{{ store.currentPaper?.title || store.currentPaper?.student_name || "未命名材料" }}</div>
      </div>
      <div class="nav">
        <span class="mono faint">{{ position }}</span>
        <button class="btn btn-sm" type="button" :disabled="!store.neighbors?.previous_paper_id" @click="goNeighbour('previous')">↑</button>
        <button class="btn btn-sm" type="button" :disabled="!store.neighbors?.next_paper_id" @click="goNeighbour('next')">↓</button>
      </div>
    </header>

    <p v-if="store.error" class="notice notice-danger bar" role="alert">{{ store.error }}</p>

    <div class="cols">
      <!-- 左：材料列表 -->
      <aside class="col materials">
        <div class="col-head mono">材料 · {{ store.papers.length }}</div>
        <button
          v-for="paper in store.papers"
          :key="paper.id"
          class="material"
          :class="{ active: paper.id === store.currentPaperId }"
          type="button"
          @click="store.selectPaper(paper.id)"
        >
          <div class="material-top">
            <span class="mono">{{ paper.student_id || "—" }}</span>
            <span class="faint mono">{{ paper.status }}</span>
          </div>
          <div class="faint material-title">{{ paper.title || paper.student_name || "—" }}</div>
        </button>
      </aside>

      <!-- 中：正文 -->
      <section class="col document">
        <p v-if="store.provenanceNotice" class="notice notice-warn doc-notice">
          {{ store.provenanceNotice }}
        </p>
        <p v-if="store.documentFrozen" class="faint mono doc-frozen">
          冻结快照 {{ store.documentView?.document_snapshot_hash?.slice(0, 12) }}…
        </p>

        <div v-if="store.documentUnavailableReason" class="notice notice-danger">
          {{ store.documentUnavailableReason }}
        </div>
        <div v-else-if="!store.documentBlocks.length" class="faint doc-empty">
          {{ store.currentRunId ? "该材料没有可显示的正文。" : "该材料尚未评分。" }}
        </div>

        <article v-else class="paper">
          <div
            v-for="block in store.documentBlocks"
            :key="block.block_id"
            class="block"
            :class="{ anchored: isAnchored(block) }"
            :data-anchor="block.chunk_id || block.evidence_unit_id"
          >
            <div v-if="block.section_title" class="block-head mono faint">
              {{ block.section_title }}
              <span v-if="block.page_start">· 第 {{ block.page_start }} 页</span>
            </div>
            <p class="block-text">
              <template v-for="(part, index) in highlight(block)" :key="index">
                <mark v-if="part.mark">{{ part.text }}</mark>
                <span v-else>{{ part.text }}</span>
              </template>
            </p>
          </div>
        </article>
      </section>

      <!-- 右：评分表 -->
      <aside class="col scores">
        <div class="col-head">
          <div class="score-head-title">评分表</div>
          <div class="faint mono">{{ store.items.length }} 个评分项</div>
        </div>

        <div v-for="item in store.items" :key="item.id" class="item" :class="{ flagged: item.need_manual_review }">
          <div class="item-top">
            <span class="item-name">{{ item.criterion_name || item.criterion_code }}</span>
            <span class="mono item-score">
              {{ item.final_score ?? item.ai_score ?? "—" }}
              <span class="faint">/ {{ item.max_score }}</span>
            </span>
          </div>

          <div class="conf">
            <span class="faint">置信度</span>
            <!-- 缺失显示「未提供」：Core 不写该字段，0% 是另一回事。 -->
            <span class="mono" :class="{ faint: item.confidence == null }">
              {{ item.confidence === null || item.confidence === undefined ? "未提供" : Math.round(item.confidence * 100) + "%" }}
            </span>
          </div>

          <p v-if="item.review_reason" class="faint item-reason">{{ item.review_reason }}</p>

          <div v-if="editing === item.id" class="edit-box">
            <label class="field">
              <span class="field-label">改为</span>
              <input
                v-model="draftScore"
                class="input"
                type="number"
                :max="item.max_score"
                min="0"
                step="0.5"
              />
            </label>
            <label class="field">
              <span class="field-label">理由（必填）</span>
              <textarea v-model="draftReason" class="input" rows="2"></textarea>
            </label>
            <div class="form-actions">
              <button
                class="btn btn-sm btn-primary"
                type="button"
                :disabled="writeBusy"
                @click="saveEdit(item)"
              >
                保存
              </button>
              <button class="btn btn-sm" type="button" @click="editing = null">取消</button>
            </div>
          </div>
          <button v-else class="btn btn-sm edit-trigger" type="button" @click="startEdit(item)">
            改分
          </button>

          <div v-if="item.evidence_view?.length" class="evidence">
            <button
              v-for="(view, index) in item.evidence_view"
              :key="index"
              class="ev"
              :class="{ dim: !locatable(view) }"
              type="button"
              :disabled="!locatable(view)"
              :title="view.unlocatable_reason || LOCATION_LABEL[view.location_status]"
              @click="onLocate(view)"
            >
              {{ evidenceChipText(view) }}
            </button>
          </div>
          <p v-else class="faint item-reason">该项没有记录证据。</p>
        </div>

        <!-- 评语与确认动作跟着内容流走，不放进 sticky 区：sticky 区一旦变高就会
             盖住上方展开的改分框，「保存」按钮点不到而页面看起来一切正常。 -->
        <div v-if="store.items.length" class="run-actions">
          <label class="field">
            <span class="field-label">评语（可选）</span>
            <textarea v-model="runNote" class="input" rows="2"></textarea>
          </label>

          <div class="form-actions">
            <button
              class="btn btn-primary"
              type="button"
              :disabled="writeBusy || !store.currentRunId"
              @click="confirmRun({ advance: true })"
            >
              确认并进入下一份
            </button>
            <button
              class="btn"
              type="button"
              :disabled="writeBusy || !store.currentRunId"
              @click="confirmRun({ advance: false })"
            >
              确认此份评分
            </button>
          </div>
          <p v-if="writeError" class="notice notice-danger" role="alert">{{ writeError }}</p>
        </div>

        <!-- 只有总分保持粘底：它是一行，高度固定，盖不住东西。 -->
        <div v-if="store.items.length" class="total">
          <div class="total-row">
            <span class="faint">总分</span>
            <span><span class="mono total-value">{{ store.totalScore }}</span><span class="faint mono"> / {{ store.maxTotal }}</span></span>
          </div>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.workspace {
  display: flex;
  flex-direction: column;
  height: 100vh;
}

.topbar {
  flex: none;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 13px 24px;
  display: flex;
  align-items: center;
  gap: 18px;
}

.back {
  border: 0;
  background: transparent;
  cursor: pointer;
  color: var(--text-muted);
  font-size: 18px;
}

.ident {
  min-width: 0;
}

.title {
  font-size: 15px;
  font-weight: 650;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.nav {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
}

.bar {
  margin: 12px 24px 0;
}

.cols {
  flex: 1;
  display: grid;
  grid-template-columns: 242px minmax(360px, 1fr) 382px;
  min-height: 0;
  overflow-x: auto;
}

.col {
  overflow-y: auto;
  min-height: 0;
}

.materials {
  border-right: 1px solid var(--border);
  background: var(--surface);
}

.col-head {
  padding: 14px 16px 10px;
  font-size: 11px;
  letter-spacing: 0.1em;
  color: var(--text-faint);
}

.score-head-title {
  font-size: 14px;
  font-weight: 650;
  letter-spacing: normal;
  color: var(--text);
}

.material {
  display: block;
  width: 100%;
  text-align: left;
  border: 0;
  border-left: 2px solid transparent;
  border-bottom: 1px solid var(--border-row);
  background: var(--surface);
  cursor: pointer;
  padding: 11px 16px;
}

.material:hover {
  background: var(--surface-muted);
}

.material.active {
  background: var(--accent-surface);
  border-left-color: var(--accent);
}

.material-top {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  margin-bottom: 4px;
}

.material-title {
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.document {
  background: var(--surface-sunken);
  padding: 20px 24px;
}

.doc-notice {
  margin-bottom: 14px;
}

.doc-frozen {
  font-size: 11px;
  margin: 0 0 14px;
}

.doc-empty {
  padding: 40px 0;
  text-align: center;
}

.paper {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  box-shadow: var(--shadow-page);
  padding: 32px 40px;
}

.block {
  padding: 8px 12px;
  margin: 0 -12px 14px;
  border-radius: 6px;
  border-left: 3px solid transparent;
}

.block.anchored {
  border-left-color: var(--warn);
  background: var(--warn-quote-bg);
}

.block-head {
  font-size: 11px;
  margin-bottom: 8px;
}

.block-text {
  margin: 0;
  font-size: 13.5px;
  line-height: 1.9;
  white-space: pre-wrap;
}

mark {
  background: var(--warn-bg);
  color: var(--warn-ink);
  padding: 1px 2px;
  border-radius: 3px;
}

.scores {
  border-left: 1px solid var(--border);
  background: var(--surface);
}

.item {
  padding: 14px 20px;
  border-bottom: 1px solid var(--border-row);
}

.item.flagged {
  background: var(--warn-surface);
}

.item-top {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}

.item-name {
  font-size: 13.5px;
  font-weight: 550;
}

.item-score {
  font-size: 15px;
}

.conf {
  display: flex;
  justify-content: space-between;
  font-size: 11.5px;
  margin-bottom: 8px;
}

.item-reason {
  font-size: 12px;
  line-height: 1.6;
  margin: 0 0 8px;
}

.evidence {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.ev {
  font-size: 12px;
  color: var(--text-secondary);
  border: 1px solid var(--border-input);
  border-radius: 5px;
  padding: 2px 8px;
  background: var(--surface);
  cursor: pointer;
}

.ev:hover:not(:disabled) {
  border-color: var(--border-input-hover);
}

.ev.dim {
  color: var(--text-faint);
  cursor: not-allowed;
}

.run-actions {
  padding: 14px 20px;
  border-top: 1px solid var(--border-row);
}

.total {
  position: sticky;
  bottom: 0;
  background: var(--surface);
  border-top: 1px solid var(--border);
  padding: 15px 20px;
}

.total-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}

.total-value {
  font-size: 26px;
  font-weight: 500;
}

@media (max-width: 1100px) {
  .cols {
    grid-template-columns: 1fr;
    overflow-x: visible;
  }

  .materials,
  .scores {
    border: 0;
    border-bottom: 1px solid var(--border);
  }
}
</style>
