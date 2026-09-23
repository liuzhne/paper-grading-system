<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  session: { type: Object, required: true },
  busy: { type: Boolean, default: false },
  rulesFile: { type: Object, default: null },
  templateFile: { type: Object, default: null },
  sourcePreview: { type: Object, default: null },
  reuploadPreview: { type: Object, default: null },
  persisted: { type: Boolean, default: false },
  readonly: { type: Boolean, default: false },
  initial: { type: Boolean, default: false },
  blockingMessage: { type: String, default: "" },
});
const emit = defineEmits([
  "pick-rules", "pick-template", "preview-source", "reupload", "update-session",
  "update-criterion", "add-criterion", "delete-criterion", "resolve-conflict", "confirm", "cancel",
  "confirm-reupload", "cancel-reupload", "close-source-preview",
]);

const activeCriteria = computed(() => (props.session.criteria || []).filter((item) => !item.deleted));
const scoreSum = computed(() => activeCriteria.value.reduce((sum, item) => sum + Number(item.max_score || 0), 0));
const unresolvedConflicts = computed(() => (props.session.conflicts || []).filter((item) => !item.resolved));
const scoreMismatch = computed(() => !props.initial && scoreSum.value !== Number(props.session.total_score));
const rulesName = computed(() => props.rulesFile?.name || props.session.files?.rules || null);
const templateName = computed(() => props.templateFile?.name || props.session.files?.template || null);
const editingDisabled = computed(() => props.busy || props.readonly || !!props.reuploadPreview);
const fileError = ref("");
const dragging = ref("");
const root = ref(null);
const highlightedCode = ref(null);
function locateScore(code = null) {
  const row = code && Array.from(root.value?.querySelectorAll('[data-criterion-code]') || [])
    .find(item => item.getAttribute('data-criterion-code') === code);
  const input = row ? row.querySelector('input.score-input') : root.value?.querySelector('[aria-label="标准满分"]');
  highlightedCode.value = code;
  input?.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
  input?.focus();
}
const positiveInteger = (value) => Number.isSafeInteger(Number(value)) && Number(value) > 0;
const validationMessage = computed(() => {
  if (props.readonly) return "";
  if (props.blockingMessage) return props.blockingMessage;
  if (props.reuploadPreview) return "请先确认或取消文件替换，再继续下一步。";
  if (!String(props.session.name || "").trim()) return "请填写标准名称。";
  if (props.initial) return rulesName.value || templateName.value ? "" : "请选择评分标准文档或评分表。";
  if (!activeCriteria.value.length) return "请添加至少一个评分项。";
  if (!positiveInteger(props.session.total_score) || activeCriteria.value.some((item) => !positiveInteger(item.max_score))) {
    return "满分与评分项分值都需要填写大于 0 的整数。";
  }
  if (activeCriteria.value.some((item) => !String(item.code || "").trim() || !String(item.name || "").trim())) {
    return "请补齐每个评分项的编号和名称。";
  }
  const codes = activeCriteria.value.map((item) => String(item.code).trim());
  if (new Set(codes).size !== codes.length) return "评分项编号不能重复。";
  if (scoreMismatch.value) return "请使评分项合计与满分一致。";
  if (unresolvedConflicts.value.length) return "请确认不同文件中不一致的内容。";
  return "";
});
const confirmDisabled = computed(() => props.busy || !!validationMessage.value);
const confirmLabel = computed(() => {
  if (props.busy) return props.initial ? "正在解析…" : "正在保存…";
  if (props.readonly) return "查看评分规则";
  if (props.initial) return "解析文件";
  return props.persisted ? "保存评分项，下一步" : "确认评分项，下一步";
});

function statusLabel(item) {
  return ({ rounded: "分值已换算", manual: "人工新增", changed_requires_confirmation: "变化待确认",
    added_requires_confirmation: "新增待补规则", removed: "已从新文件移除" })[item.parse_status]
    || (item.source_refs?.length ? "按原文解析" : "出处待补充");
}

function formatLocator(locator) {
  if (locator == null) return "";
  if (typeof locator !== "object") return String(locator);
  return [...new Set(Object.values(locator).flatMap((value) => {
    const text = formatLocator(value);
    return text ? [text] : [];
  }))].join(" · ");
}

function sourceLabel(item) {
  const source = item.source_refs?.[0];
  return formatLocator(source?.locator) || source?.sheet_name || source?.section_path?.join?.(" · ")
    || (item.parse_status === "manual" ? "人工新增" : "来源待补充");
}

function fileNote(kind) {
  const selected = kind === "word" ? props.templateFile : props.rulesFile;
  const saved = kind === "word" ? props.session.files?.template : props.session.files?.rules;
  if (selected && selected.name !== saved) return props.initial ? "已选择 · 点击右侧解析文件" : "已选择新文件 · 待确认替换";
  if (props.initial) return "已选择 · 点击右侧解析文件";
  const metadata = props.session.file_metadata?.[kind === "word" ? "template" : "rules"];
  const bytes = metadata?.size_bytes ?? selected?.size;
  const size = Number.isFinite(bytes) ? (bytes < 1024 ? `${bytes} B` : `${Math.round(bytes / 1024)} KB`) : "";
  const uploaded = metadata?.uploaded_at ? new Date(metadata.uploaded_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
  return [size, uploaded && `${uploaded} 上传`, "已解析"].filter(Boolean).join(" · ");
}

function chooseFile(kind, event) {
  if (editingDisabled.value) return;
  fileError.value = "";
  emit(kind === "word" ? "pick-template" : "pick-rules", event);
}

function dropFile(kind, event) {
  dragging.value = "";
  if (editingDisabled.value) return;
  const file = event.dataTransfer?.files?.[0];
  if (!file) return;
  const accepted = kind === "word" ? /\.docx$/i : /\.(xlsx|xlsm)$/i;
  if (!accepted.test(file.name)) {
    fileError.value = kind === "word" ? "评分标准文档请选择 .docx 文件。" : "评分表请选择 .xlsx 或 .xlsm 文件。";
    return;
  }
  chooseFile(kind, { target: { files: [file] } });
}

function updateMeta(field, event) {
  if (editingDisabled.value) return;
  const value = field === "total_score" ? Number(event.target.value) : event.target.value;
  emit("update-session", { [field]: value });
}

function updateCriterion(index, field, event) {
  if (editingDisabled.value || (props.persisted && field === "code")) return;
  const value = field === "max_score" ? Number(event.target.value) : event.target.value;
  emit("update-criterion", { index, field, value });
}
</script>

<template>
  <section ref="root" class="import-workspace" data-test="rubric-import-workspace">
    <div class="workspace-main">
      <section class="card upload-card">
        <div class="card-head-line">
          <div><h2 class="card-title">评分标准文档 <span class="file-kind">docx</span></h2>
            <p class="card-note">上传评分标准原文，提取评分项、分值与文字描述，保留原文出处。</p></div>
        </div>
        <div class="file-row" :class="{ 'drop-active': dragging === 'word' }" data-test="word-dropzone"
          @dragover.prevent="!editingDisabled && (dragging = 'word')" @dragleave.prevent="dragging = ''" @drop.prevent="dropFile('word', $event)">
          <div class="file-details"><span class="file-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h4"/></svg></span>
            <div><strong>{{ templateName || "尚未上传评分标准文档" }}</strong>
            <p class="faint">{{ templateName ? fileNote('word') : readonly ? "此标准未保留 Word 来源文件" : "拖入 .docx 文件，或点击选择" }}</p></div></div>
          <div class="file-actions">
            <button v-if="session.files?.template && !initial" class="btn btn-sm" type="button" :disabled="busy" @click="emit('preview-source', 'word')">预览原文</button>
            <label v-if="!readonly" class="btn btn-sm file-button" :class="{ disabled: editingDisabled }">{{ templateName ? "重新上传" : "选择文件" }}
              <input aria-label="评分标准文档" type="file" accept=".docx" :disabled="editingDisabled" @change="chooseFile('word', $event)" />
            </label>
          </div>
        </div>
      </section>

      <section class="card upload-card">
        <div class="card-head-line"><div><h2 class="card-title">评分表 <span class="file-kind">xlsx</span><span class="faint optional">可选</span></h2>
          <p class="card-note">已有评分项和分值的表格。与文档内容不一致时，会列出差异供你确认。</p></div></div>
        <div class="file-row" :class="{ empty: !rulesName, 'drop-active': dragging === 'excel' }" data-test="excel-dropzone"
          @dragover.prevent="!editingDisabled && (dragging = 'excel')" @dragleave.prevent="dragging = ''" @drop.prevent="dropFile('excel', $event)">
          <div><strong>{{ rulesName || (readonly ? "未提供评分表" : "拖入 .xlsx 文件，或点击选择") }}</strong>
            <p class="faint">{{ rulesName ? fileNote('excel') : readonly ? "此标准未保留 Excel 来源文件" : "首行为表头 · 需包含评分项名称与满分 · 支持 .xlsx / .xlsm" }}</p></div>
          <div class="file-actions"><button v-if="session.files?.rules && !initial" class="btn btn-sm" type="button" :disabled="busy" @click="emit('preview-source', 'excel')">预览原文</button>
          <label v-if="!readonly" class="btn btn-sm file-button" :class="{ disabled: editingDisabled, 'dropzone-picker': !rulesName }">{{ rulesName ? "重新上传" : "选择文件" }}
            <input aria-label="评分表" type="file" accept=".xlsx,.xlsm" :disabled="editingDisabled" @change="chooseFile('excel', $event)" />
          </label></div>
        </div>
      </section>

      <section v-if="sourcePreview" class="card preview-card" data-test="source-preview">
        <div class="criteria-head"><div><h2 class="card-title">{{ sourcePreview.document === 'word' ? 'Word' : 'Excel' }} 原文预览</h2>
          <p class="card-note">对照原文核对评分项、分值和出处。</p></div>
          <button class="btn btn-sm" type="button" @click="emit('close-source-preview')">关闭</button></div>
        <p v-if="!sourcePreview.items?.length" class="card-note preview-empty">这份标准尚无可预览的原文记录。</p>
        <ol class="source-list"><li v-for="item in sourcePreview.items || []" :key="item.unit_id"><span class="faint">{{ formatLocator(item.locator) || item.unit_id }}</span><p>{{ item.text }}</p></li></ol>
      </section>

      <section v-if="reuploadPreview" class="card preview-card" data-test="reupload-preview">
        <div class="criteria-head"><div><h2 class="card-title">重新上传差异</h2>
          <p class="card-note">确认后才替换当前草稿；已发布版本不会被覆盖。</p></div></div>
        <p v-if="persisted" class="preview-empty faint">保留已确认规则 {{ reuploadPreview.retained_rules_count || 0 }} 条；需要重新确认 {{ reuploadPreview.invalidated_rules_count || 0 }} 条。</p>
        <ul class="diff-list"><li v-for="item in reuploadPreview.criteria_diff || []" :key="item.code">
          <strong>{{ item.code }} · {{ item.name }}</strong>
          <span>{{ { added: '新增', removed: '移除', modified: '变化', unchanged: '未变化' }[item.change_type] }}</span>
          <span v-if="item.old_max_score !== item.new_max_score">{{ item.old_max_score ?? '—' }} → {{ item.new_max_score ?? '—' }}</span>
        </li></ul>
        <div class="reupload-actions"><button class="btn" type="button" :disabled="busy" @click="emit('cancel-reupload')">取消替换</button>
          <button data-test="confirm-reupload" class="btn btn-primary" type="button" :disabled="busy || readonly" @click="emit('confirm-reupload')">确认替换草稿</button></div>
      </section>

      <section class="card criteria-card">
        <div class="criteria-head"><div><h2 class="card-title">解析出的评分项</h2>
          <p class="card-note">共 {{ activeCriteria.length }} 项 · 合计 {{ Number.isFinite(scoreSum) ? scoreSum : '—' }} 分</p></div>
          <button v-if="!readonly && !persisted && !initial" class="btn btn-sm" type="button" :disabled="editingDisabled" @click="emit('add-criterion')">新增评分项</button>
          <span v-else-if="persisted && !readonly" class="faint criteria-hint">增删评分项请重新上传文件</span></div>
        <div class="criteria-table-wrap">
          <table class="criteria-table">
            <thead><tr><th class="code-col">编号</th><th>评分项名称</th><th class="score-col">满分</th><th>文档出处</th><th>解析结果</th><th v-if="!persisted && !readonly && !initial">操作</th></tr></thead>
            <tbody>
              <tr v-if="!activeCriteria.length"><td :colspan="persisted || readonly || initial ? 5 : 6" class="empty-criteria">{{ initial ? '上传文件后，解析出的评分项会显示在这里' : '暂无评分项，可添加评分项或重新上传文件。' }}</td></tr>
              <tr v-for="(item, index) in session.criteria" v-show="!item.deleted" :key="`${item.code}-${index}`" :data-criterion-code="item.code" :class="{ highlighted: highlightedCode === item.code }">
                <td><input class="input cell-input code" :aria-label="`${item.code || index + 1} 评分项编号`" :value="item.code" :disabled="editingDisabled || persisted" @change="updateCriterion(index, 'code', $event)" /></td>
                <td><div v-if="item.dimension" class="criterion-dimension">{{ item.dimension }}</div>
                  <label class="criterion-name-row" :class="{ hierarchical: item.dimension }"><span v-if="item.dimension">子项</span><input class="input cell-input name" :aria-label="`${item.code || index + 1} 评分项名称`" :value="item.name" :disabled="editingDisabled" @change="updateCriterion(index, 'name', $event)" /></label>
                  <details class="criterion-description"><summary>{{ item.description || '评分项说明' }}</summary><textarea class="input" rows="3" :aria-label="`${item.code || index + 1} 评分项说明`" :value="item.description || ''" :disabled="editingDisabled" @change="updateCriterion(index, 'description', $event)" /></details></td>
                <td><input class="input cell-input score-input" :aria-label="`${item.code || index + 1} 满分`" type="number" min="1" step="1" :value="item.max_score" :disabled="editingDisabled" @change="updateCriterion(index, 'max_score', $event)" /></td>
                <td class="source-cell faint" :aria-label="`${item.code || index + 1} 文档出处`">{{ sourceLabel(item) }}</td>
                <td><span class="chip" :class="['rounded', 'changed_requires_confirmation', 'added_requires_confirmation'].includes(item.parse_status) ? 'chip-warn' : 'chip-ok'">{{ statusLabel(item) }}</span></td>
                <td v-if="!persisted && !readonly && !initial"><button class="link-danger" type="button" :aria-label="`删除 ${item.code || index + 1}`" :disabled="editingDisabled" @click="emit('delete-criterion', index)">删除</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
      <slot name="analysis" />
    </div>

    <aside class="workspace-side">
      <section class="card side-card">
        <h2 class="card-title">基本信息</h2>
        <label class="field"><span class="field-label">标准名称</span><input aria-label="标准名称" class="input" placeholder="填写评分标准名称" :value="session.name" :disabled="editingDisabled" @input="(initial || persisted) && updateMeta('name', $event)" @change="!initial && !persisted && updateMeta('name', $event)" /></label>
        <label v-if="!initial" class="field"><span class="field-label">满分</span><input aria-label="标准满分" class="input" type="number" min="1" step="1" :value="session.total_score" :disabled="editingDisabled" @change="updateMeta('total_score', $event)" /></label>
        <p v-else class="card-note">解析后按评分项计算满分，你可以核对并调整。</p>
        <p v-if="initial" class="card-note">默认仅自己可见，发布时再选择分享范围。</p>
        <details class="more-info"><summary>更多信息</summary>
          <label class="field"><span class="field-label">版本</span><input aria-label="版本" class="input" :value="session.version" :disabled="editingDisabled" @input="(initial || persisted) && updateMeta('version', $event)" @change="!initial && !persisted && updateMeta('version', $event)" /></label>
          <label class="field"><span class="field-label">标准说明</span><textarea aria-label="标准说明" class="input" rows="3" :value="session.description || ''" :disabled="editingDisabled" @input="(initial || persisted) && updateMeta('description', $event)" @change="!initial && !persisted && updateMeta('description', $event)" /></label>
        </details>
      </section>

      <section v-if="scoreMismatch || session.score_adjustments?.length || unresolvedConflicts.length" class="card side-card issues" role="alert">
        <h2 class="card-title">解析需要确认 {{ (session.score_adjustments?.length || 0) + unresolvedConflicts.length + (scoreMismatch ? 1 : 0) }} 处</h2>
        <button v-if="scoreMismatch" data-test="fix-total" class="issue-link" type="button" @click="locateScore()">评分项合计 {{ scoreSum }} 分，与满分 {{ session.total_score }} 分不一致。</button>
        <button v-for="item in session.score_adjustments || []" :key="item.code" :data-test="`rounding-${item.code}`" class="issue-link" type="button" @click="locateScore(item.code)">{{ item.message }}</button>
        <div v-for="(item, index) in unresolvedConflicts" :key="item.anchor_unit_id || index" class="conflict-item">
          <p>{{ item.message || `${item.type || '来源'}存在冲突` }}</p>
          <div class="conflict-actions"><button data-test="resolve-use-excel" class="btn btn-sm" type="button" :disabled="editingDisabled" @click="emit('resolve-conflict', { conflict: item, decision: 'use_excel' })">以 Excel 为准</button>
            <button data-test="resolve-use-word" class="btn btn-sm" type="button" :disabled="editingDisabled" @click="emit('resolve-conflict', { conflict: item, decision: 'use_word' })">采用 Word 内容</button></div>
        </div>
      </section>
      <slot name="issues" />
      <p v-if="fileError" class="validation-note" role="alert">{{ fileError }}</p>
      <p v-if="validationMessage" class="validation-note" role="status">{{ validationMessage }}</p>

      <button data-test="confirm-import-session" class="btn btn-primary confirm-button" type="button" :disabled="confirmDisabled" @click="emit('confirm')">
        {{ confirmLabel }}
      </button>
      <button v-if="!persisted && !readonly" class="btn cancel-button" type="button" :disabled="busy" @click="emit('cancel')">取消导入</button>
    </aside>
  </section>
</template>

<style scoped>
.import-workspace { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 20px; align-items: start; }
.preview-empty { margin: 0 22px 18px; }
.highlighted { background: #fff8e9; }
.cell-input { height: auto; }
.issue-link { display: block; width: 100%; padding: 7px 0; border: 0; background: none; color: #93621f; font: inherit; font-size: 12px; text-align: left; line-height: 1.7; cursor: pointer; }
.workspace-main, .workspace-side { display: flex; flex-direction: column; gap: 18px; min-width: 0; }
.upload-card, .side-card { padding: 22px; }
.card { background: #fff; border: 1px solid #e3e3df; border-radius: 12px; box-shadow: none; margin-bottom: 0; }
.criterion-dimension { margin-bottom: 2px; color: #30372f; font-size: 13px; font-weight: 650; }.criterion-name-row { display: block; }.criterion-name-row.hierarchical { display: flex; align-items: center; gap: 6px; color: #8e948c; font-size: 11px; }.criterion-name-row.hierarchical .cell-input.name { min-width: 118px; color: #697168; font-size: 12px; font-weight: 500; }.criterion-description { max-width: 440px; font-size: 11px; color: #8e948c; }.criterion-description summary { overflow: hidden; cursor: pointer; text-overflow: ellipsis; white-space: nowrap; }.criterion-description textarea { margin-top: 8px; min-width: 180px; }
.card-head-line, .criteria-head, .file-row, .file-actions { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.card-title { margin: 0; font-size: 15px; font-weight: 600; }.card-note { margin: 7px 0 0; font-size: 12px; line-height: 1.6; color: #91958e; }.file-kind { margin-left: 6px; color: #a1a49e; font: 11px monospace; font-weight: 400; }
.faint { color: #969b94; font-size: 12px; }.btn { white-space: nowrap; }.file-details { display: flex; align-items: center; gap: 12px; min-width: 0; }.file-details > div { min-width: 0; }.file-row strong { display: block; overflow-wrap: anywhere; font-size: 13px; font-weight: 500; }.file-icon { display: flex; align-items: center; justify-content: center; flex: 0 0 32px; height: 32px; color: #175c50; background: #edf3f0; border-radius: 6px; }.file-icon svg { width: 18px; height: 18px; }
.optional { margin-left: 10px; font-size: 12px; }.file-row { margin-top: 18px; padding: 16px 18px; border: 1px solid #e3e6e1; border-radius: 10px; }
.file-row.empty { position: relative; min-height: 92px; border-style: dashed; background: #faf9f6; justify-content: center; text-align: center; }.file-row.empty .file-actions { position: static; }.file-row.empty .dropzone-picker { position: absolute; inset: 0; width: 100%; border: 0; background: transparent; color: transparent; box-shadow: none; }.file-row.empty .file-actions { width: 0; }.file-row.drop-active { border-color: #175c50; background: #eff6f3; }.file-row p { margin: 5px 0 0; line-height: 1.6; }.file-button { position: relative; overflow: hidden; cursor: pointer; }.file-button:focus-within { outline: 2px solid #175c50; outline-offset: 2px; }.file-button.disabled { opacity: .55; cursor: default; }
.file-button input { position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%; }.file-button input:disabled { cursor: default; }.criteria-card { overflow: hidden; }.criteria-head { padding: 20px 22px; }.criteria-hint { text-align: right; }
.criteria-table-wrap { overflow-x: auto; }.criteria-table { width: 100%; border-collapse: collapse; min-width: 570px; table-layout: auto; }
.criteria-table th, .criteria-table td { padding: 10px 12px; border-top: 1px solid #eeefec; text-align: left; vertical-align: middle; }.criteria-table th:first-child, .criteria-table td:first-child { padding-left: 22px; }.criteria-table th:last-child, .criteria-table td:last-child { padding-right: 22px; }.criteria-table tbody tr { height: 53px; }
.criteria-table th { color: #969b94; background: #faf9f6; font-size: 12px; font-weight: 500; }.code-col { width: 74px; }.score-col { width: 70px; }.cell-input { width: 100%; min-width: 48px; border: 1px solid transparent; background: transparent; padding: 6px 0; border-radius: 5px; font: inherit; font-size: 13px; color: #30372f; }.cell-input.code { width: 56px; color: #8e948c; font: 12px monospace; }.cell-input.name { min-width: 112px; font-weight: 600; }
.cell-input:focus, .cell-input:hover:not(:disabled) { border-color: #cdd5d0; background: white; }.cell-input:disabled { opacity: 1; -webkit-text-fill-color: currentColor; }.score-input { width: 56px; }.source-cell { max-width: 180px; overflow-wrap: anywhere; font: 12px monospace; }.link-danger { border: 0; background: transparent; cursor: pointer; font-size: 12px; }
.link-danger { color: #a84638; }.issues { border-color: #f0e2c9; background: #fffbf4; }.issues .card-title { color: #a77428; font-size: 13px; }.issues .card-title::before { content: '●'; font-size: 9px; margin-right: 8px; }.issues p { line-height: 1.7; font-size: 12px; color: #8c897c; }.score-adjustment { margin: 10px 0 0; }.chip { display: inline-block; white-space: nowrap; border-radius: 4px; padding: 4px 8px; font-size: 11px; font-weight: 400; }.chip-ok { background: #eaf1ed; color: #4c7c6e; }.chip-warn { background: #f8eedb; color: #a57c3a; }.criteria-table .empty-criteria { height: 146px; text-align: center; color: #969b94; font-size: 13px; }
.confirm-button, .cancel-button { width: 100%; justify-content: center; min-height: 46px; }.field { margin-top: 16px; }
.more-info { margin-top: 20px; font-size: 12px; color: #8d928a; }.more-info summary { cursor: pointer; }.validation-note { margin: -4px 0; font-size: 12px; color: #947539; line-height: 1.6; }.cancel-button { border: 0; background: transparent; color: #8d928a; min-height: 30px; margin-top: -12px; }.side-card .field { display: flex; flex-direction: column; gap: 7px; }.field-label { color: #5e655b; font-size: 12px; font-weight: 500; }.side-card .input { min-width: 0; width: 100%; }
.preview-card { padding-bottom: 20px; }.source-list { max-height: 320px; overflow: auto; margin: 0 24px; padding-left: 20px; }.source-list p { margin: 4px 0 14px; white-space: pre-wrap; }
.diff-list { margin: 0 24px 16px; padding: 0; list-style: none; }.diff-list li { display: grid; grid-template-columns: minmax(160px, 1fr) 80px 100px; gap: 12px; padding: 10px 0; border-top: 1px solid #eceeea; }
.reupload-actions, .conflict-actions { display: flex; justify-content: flex-end; gap: 10px; padding: 0 24px; }.conflict-item { padding: 8px 0; border-top: 1px solid #ead8b9; }.conflict-item p { margin: 4px 0 8px; }.conflict-actions { justify-content: flex-start; padding: 0; flex-wrap: wrap; }
@media (max-width: 1100px) { .import-workspace { grid-template-columns: minmax(0, 1fr) 240px; gap: 16px; }.upload-card, .side-card { padding: 18px; }.file-row { padding: 14px; }.file-actions { gap: 6px; } }
@media (max-width: 900px) { .import-workspace { grid-template-columns: minmax(0, 1fr); }.workspace-side { display: grid; grid-template-columns: minmax(0, 1fr); }.confirm-button, .cancel-button { width: 100%; } }
@media (max-width: 640px) { .file-row, .criteria-head { align-items: flex-start; flex-direction: column; }.file-actions { width: 100%; flex-wrap: wrap; }.upload-card, .side-card { padding: 18px; } }
</style>
