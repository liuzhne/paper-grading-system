<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import { materialIdentity } from "@/lib/document-types.js";
import { deductionReasons } from "@/lib/score-items.js";
import { useGradingStore } from "@/stores/grading.js";

const store = useGradingStore();
const route = useRoute();
const router = useRouter();

/** 首次加载、材料还没回来：此时显示“0 份”“未命名材料”会被读成批次是空的。 */
const firstLoad = computed(() => store.loading && !store.papers.length);

const position = computed(() =>
  store.neighbors ? `${store.neighbors.position} / ${store.neighbors.total}` : "—",
);

/** 中栏三页签（设计稿）。正文之外的两个读的是评分时已经产出的检查发现。 */
const TABS = [
  { key: "text", label: "正文" },
  { key: "coherence", label: "篇章结构" },
  { key: "format", label: "格式发现" },
];

const EMPTY_TEXT = {
  // 空表说明不了「查过但没问题」还是「压根没查」。这两句话分别对应两种情况。
  coherence: "未发现确定性一致性问题（图表引用、编号制引文与参考文献核对通过）。",
  format: "未发现格式问题（或模板未规定格式，非 docx 提交无法判定）。",
};

const SEVERITY_LABEL = { error: "错误", warn: "提醒", info: "提示" };
const SEVERITY_TONE = { error: "chip-danger", warn: "chip-warn" };

const docTab = ref("text");

function findingsOf(key) {
  if (key === "coherence") return store.coherenceFindings;
  if (key === "format") return store.formatFindings;
  return [];
}

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
// 改分的失败提示放在「保存」旁边：放在评分表底部时，用户在按钮附近看不到任何
// 反应，以为点了没用（2026-10-19 生产反馈）。
const editError = ref(null);
const runNote = ref("");

/**
 * AI 没有评出有效结果的项（`invalid` / `blocked`）：普通改分会被后端拒绝
 * （thesis_artifacts.assert_ordinary_item_override_allowed），这份评分也确认不了
 * （总分为空）。不摆「改分」「确认」这种必然失败的入口，当场说明并指向「重试失败项」。
 */
const UNRESOLVED_AUTO_STATUSES = new Set(["invalid", "blocked"]);
function unresolved(item) {
  return UNRESOLVED_AUTO_STATUSES.has(item.auto_score_status);
}
const unresolvedCount = computed(() => store.items.filter(unresolved).length);

/**
 * 后端的拒绝原因是给开发看的英文；这里译成老师能照着做的中文。
 * 没对上的按 HTTP 状态给一句通用说明，不把英文原文直接摆给用户。
 */
const WRITE_ERRORS = [
  [/invalid or blocked auto score/i, () => "AI 没有评出这一项的有效结果，不能直接改分；请到评分进度页「重试失败项」重新评分。"],
  [/invalid or blocked score items remain/i, () => "还有 AI 没评出有效结果的项，暂时不能确认这份评分；请先到评分进度页「重试失败项」。"],
  [/final_score out of range/i, (item) => `分数要在 0 到 ${item?.max_score ?? "满分"} 之间。`],
  [/no frozen policy snapshot/i, () => "这份评分来自旧版本，不能重算；请重新评分后再改。"],
  [/score item not found|scoring run not found/i, () => "这一项已不存在，可能材料被重新评分了；请刷新页面。"],
];

function describeWriteError(err, fallback, item = null) {
  const message = err instanceof Error ? err.message : "";
  for (const [pattern, text] of WRITE_ERRORS) {
    if (pattern.test(message)) return text(item);
  }
  // 已经是中文的（归档批次、理由必填、权限）原样给出。
  if (/[\u4e00-\u9fff]/.test(message)) return message;
  // 并发冲突：别人刚改过。不能叫用户「稍后重试」——重试会覆盖别人的改动，
  // 要先刷新看清当前分数再决定。
  if (err?.status === 409) return "这份评分刚被别人改过，请刷新页面、看清当前分数后再操作。";
  const status = err?.status ? `（HTTP ${err.status}）` : "";
  return `${fallback}${status}，请稍后重试；问题持续请联系管理员。`;
}

function startEdit(item) {
  editing.value = item.id;
  draftScore.value = item.final_score ?? item.ai_score ?? 0;
  draftReason.value = "";
  editError.value = null;
}

async function saveEdit(item) {
  const raw = draftScore.value;
  const score = Number(raw);
  if (raw == null || raw === "" || !Number.isFinite(score) || score < 0 || score > Number(item.max_score)) {
    editError.value = `分数要在 0 到 ${item.max_score} 之间。`;
    return;
  }
  writeBusy.value = true;
  editError.value = null;
  try {
    await store.overrideScore(item.id, { score, reason: draftReason.value });
    editing.value = null;
  } catch (err) {
    // 并发冲突要让用户看到并重新决定，不静默覆盖别人的改动。
    editError.value = describeWriteError(err, "改分没有保存", item);
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
    writeError.value = describeWriteError(err, "提交复核失败");
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
  // `?paper=` 来自复核队列的「查看原文」。没有它就落在列表首项——用户点的是
  // 某一行，打开的却是另一份材料，而页面上没有任何东西说明这件事。
  store.openBatch(route.params.batchId, route.query.paper || null);
  window.addEventListener("keydown", onKeydown);
});

onUnmounted(() => window.removeEventListener("keydown", onKeydown));
</script>

<template>
  <div class="workspace">
    <header class="topbar">
      <button class="back" type="button" @click="router.push({ name: 'tasks' })">←</button>
      <div class="ident">
        <div class="faint mono">{{ firstLoad ? "" : materialIdentity(store.currentPaper, store.documentType).code }}</div>
        <div class="title">{{ firstLoad ? "正在加载…" : materialIdentity(store.currentPaper, store.documentType).title }}</div>
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
        <div class="col-head mono">材料 · {{ firstLoad ? "…" : store.papers.length }}</div>
        <p v-if="firstLoad" class="faint list-loading" role="status">正在加载材料…</p>
        <button
          v-for="paper in store.papers"
          :key="paper.id"
          class="material"
          :class="{ active: paper.id === store.currentPaperId }"
          type="button"
          @click="store.selectPaper(paper.id)"
        >
          <div class="material-top">
            <span class="mono">{{ materialIdentity(paper, store.documentType).code }}</span>
            <span class="faint mono">{{ paper.status }}</span>
          </div>
          <div class="faint material-title">{{ materialIdentity(paper, store.documentType, "—").title }}</div>
        </button>
      </aside>

      <!-- 中：正文 / 篇章结构 / 格式发现 -->
      <section class="col document">
        <!-- 页签上带条数：不点开就能知道有没有东西，省掉一次「切过去看看」。 -->
        <div class="doc-tabs" role="tablist" aria-label="正文与检查发现">
          <button
            v-for="tab in TABS"
            :key="tab.key"
            class="doc-tab"
            :class="{ on: docTab === tab.key }"
            type="button"
            role="tab"
            :aria-selected="docTab === tab.key"
            @click="docTab = tab.key"
          >
            {{ tab.label }}
            <span v-if="tab.key !== 'text'" class="faint mono tab-count">
              {{ findingsOf(tab.key).length }}
            </span>
          </button>
        </div>

        <template v-if="docTab === 'text'">
          <p v-if="store.provenanceNotice" class="notice notice-warn doc-notice">
            {{ store.provenanceNotice }}
          </p>
          <p v-if="store.documentFrozen" class="faint mono doc-frozen">
            冻结快照 {{ store.documentView?.document_snapshot_hash?.slice(0, 12) }}…
          </p>

          <div v-if="store.documentUnavailableReason" class="notice notice-danger">
            {{ store.documentUnavailableReason }}
          </div>
          <div v-else-if="store.loading" class="faint doc-empty" role="status">正在加载…</div>
          <div v-else-if="store.currentRunId && !store.documentBlocks.length" class="faint doc-empty">
            该材料没有可显示的正文。
          </div>
          <!-- 没有评分结果有两种来由：还没开始评，或评分失败没有产出。原因与重新评分都在评分进度页。 -->
          <div v-else-if="!store.currentRunId && store.currentPaper" class="faint doc-empty" data-test="no-result">
            该材料还没有评分结果。
            <RouterLink :to="{ name: 'task-run', params: { batchId: route.params.batchId } }">到评分进度页</RouterLink>
            查看原因或开始评分。
          </div>
          <div v-else-if="!store.documentBlocks.length" class="faint doc-empty">这个评分任务里还没有材料。</div>

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
        </template>

        <!-- 发现项。这两组数据一直随 run 回到浏览器里，也进了 HTML 报告与 JSON
             导出，只有工作区从来没显示过——评分时扣了分，界面上看不到扣在哪。 -->
        <template v-else>
          <p v-if="!findingsOf(docTab).length" class="faint doc-empty">
            {{ EMPTY_TEXT[docTab] }}
          </p>
          <div v-else class="table-wrap findings">
            <table class="table">
              <thead>
                <tr>
                  <th>级别</th>
                  <th>{{ docTab === "format" ? "项" : "类型" }}</th>
                  <th>说明</th>
                  <th>计入扣分</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(finding, index) in findingsOf(docTab)" :key="index">
                  <td>
                    <span class="chip" :class="SEVERITY_TONE[finding.severity] || ''">
                      {{ SEVERITY_LABEL[finding.severity] || finding.severity || "—" }}
                    </span>
                  </td>
                  <td class="mono field">{{ finding.field || finding.kind || "—" }}</td>
                  <td class="muted">{{ finding.message || "—" }}</td>
                  <!-- 「已经扣过分」和「只是提示」是两件事。留空会被当成后者，
                       于是同一个问题在人工复核时又被扣一次。 -->
                  <td>
                    <template v-if="finding.deducted_by">
                      <span class="mono">{{ finding.deducted_by }}</span>
                      <span v-if="finding.deducted_points != null" class="mono deduct">
                        −{{ finding.deducted_points }}
                      </span>
                    </template>
                    <span v-else class="faint">未计入</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
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

          <!-- 扣分理由与修改建议：评分助手按这些说“怎么改”，老师在这里核对依据（方案 §31.3）。 -->
          <ul v-if="deductionReasons(item).length" class="item-deductions" data-test="item-deductions">
            <li v-for="(line, index) in deductionReasons(item)" :key="index">{{ line }}</li>
          </ul>
          <p v-if="item.revision_suggestion" class="item-suggestion" data-test="item-suggestion">
            <span class="faint">修改建议：</span>{{ item.revision_suggestion }}
          </p>

          <p v-if="unresolved(item)" class="notice notice-warn item-unresolved" data-test="item-unresolved">
            AI 没有评出这一项的有效结果，不能直接改分。
            <RouterLink :to="{ name: 'task-run', params: { batchId: route.params.batchId } }">到评分进度页「重试失败项」</RouterLink>
          </p>
          <div v-else-if="editing === item.id" class="edit-box">
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
            <p v-if="editError" class="notice notice-danger edit-error" role="alert" data-test="edit-error">{{ editError }}</p>
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

          <p v-if="unresolvedCount" class="notice notice-warn" data-test="run-unresolved">
            有 {{ unresolvedCount }} 项 AI 没评出有效结果，这份评分暂时不能确认。
            <RouterLink :to="{ name: 'task-run', params: { batchId: route.params.batchId } }">到评分进度页「重试失败项」</RouterLink>
          </p>
          <div class="form-actions">
            <button
              class="btn btn-primary"
              type="button"
              :disabled="writeBusy || !store.currentRunId || unresolvedCount > 0"
              @click="confirmRun({ advance: true })"
            >
              确认并进入下一份
            </button>
            <button
              class="btn"
              type="button"
              :disabled="writeBusy || !store.currentRunId || unresolvedCount > 0"
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

.doc-tabs {
  flex: none;
  display: flex;
  gap: 4px;
  align-items: center;
  border-bottom: 1px solid var(--border);
  margin-bottom: 12px;
  padding-bottom: 0;
}

.doc-tab {
  border: 0;
  background: transparent;
  padding: 8px 12px 9px;
  font: inherit;
  font-size: 13px;
  color: var(--text-muted);
  cursor: pointer;
  /* 选中态用一条下边线，不是背景块：中栏本身是阅读区，色块会跟正文抢注意力。 */
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}

.doc-tab:hover {
  color: var(--text-secondary);
}

.doc-tab.on {
  color: var(--text);
  font-weight: 600;
  border-bottom-color: var(--accent);
}

.tab-count {
  margin-left: 5px;
  font-size: 11.5px;
}

.findings .field {
  white-space: nowrap;
}

.findings .deduct {
  margin-left: 6px;
}

.list-loading {
  padding: 12px 14px;
  font-size: 12px;
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

.item-unresolved,
.edit-error {
  margin: 8px 0 0;
  font-size: 13px;
  line-height: 1.6;
}

.item-reason {
  font-size: 12px;
  line-height: 1.6;
  margin: 0 0 8px;
}

.item-deductions {
  margin: 0 0 8px;
  padding-left: 16px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--danger-ink);
}

.item-suggestion {
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.6;
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
