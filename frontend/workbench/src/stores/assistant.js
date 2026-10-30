/**
 * 评分助手（对话评分助手方案）：薄客户端。
 *
 * 流程由后端的 LangGraph 图推进；这里只做五件事：
 * - 把用户输入、工作区与卡片上的“确定”、选择卡片的操作发给 `POST /assistant/conversations/{id}/runs`；
 * - 渲染返回的消息与卡片（卡片只有类型与编号，分数与证据渲染时现取）；
 * - 做浏览器才能做的事：选文件、直传上传（`upload` store）、建评分标准导入草稿（`rubrics` store）；
 * - 轮询评分进度或评分标准状态，发现可能结束后通知图继续——是否真的结束由后端核对；
 * - 记住工作区的界面状态：看哪个标签、哪一步、收起还是全宽（v2 界面改造方案 §3.1，不落库）。
 */

import { defineStore } from "pinia";
import { reactive, ref, watch } from "vue";

import { api, ApiError, StaleContextError } from "@/api/client.js";
import { MAX_FILES, TERMINAL_JOB_STATUSES, limitFiles } from "@/lib/assistant-flow.js";
import { rememberDocumentType } from "@/lib/document-types.js";
import { useRubricsStore } from "@/stores/rubrics.js";
import { useUploadStore } from "@/stores/upload.js";

const JOB_POLL_MS = 3000;
const RUBRIC_POLL_MS = 5000;
const SETUP_DISMISSED_KEY = "pgs.assistant.setupDismissed";
/** 老师在任务标签上操作后这么久之内，新结果不抢焦点，改为顶部提示（设计稿：60 秒）。 */
const BUSY_MS = 60000;
/** 结果卡片：出现时在工作区开一个结果标签（流程里的带 `step`，是任务标签的入口，不算）。 */
const ARTIFACT_TYPES = new Set(["progress_snapshot", "job_progress", "paper_result", "overview", "review_list", "todo"]);

function errorText(err, fallback = "操作失败") {
  if (err instanceof ApiError) {
    const detail = err.detail;
    if (typeof detail === "string" && detail) return detail;
    if (detail && typeof detail === "object") {
      const text = detail.message || detail.title || detail.code;
      if (typeof text === "string" && text) return text;
    }
    return fallback;
  }
  return err?.message || fallback;
}

export const useAssistantStore = defineStore("assistant", () => {
  const settings = ref(null);
  const conversations = ref([]);
  const conversation = ref(null);
  const messages = ref([]);
  const pending = ref(null);
  const sending = ref(false);
  const uploading = ref(false);
  const error = ref(null);
  const setupOpen = ref(false);
  const workspacePath = ref(null);
  const workspaceKey = ref(0);
  /** 实时作业数据：batchId → job，供进度卡片显示。 */
  const jobs = reactive({});
  /** 本页内存里的待评文件：流程线程 → File[]。刷新后失效，卡片会请用户重新选择。 */
  const filesByThread = reactive({});
  /**
   * 工作区的界面状态（不落库）：`mode` open / closed / focus（全宽）；`tab` 是 "task" 或结果卡片编号；
   * `step` 是老师点开的步骤，null 表示跟着当前步骤；`toast` 是没抢焦点的新结果；`narrow` 由页面按窗口宽度设置。
   */
  const ws = reactive({ mode: "open", tab: "task", step: /** @type {string|null} */ (null),
    toast: /** @type {string|null} */ (null), lastTaskEdit: 0, narrow: false });
  /**
   * 附件触发了“要放弃上一步吗”的确认卡片时，新流程还没开始、没有线程：文件按那张卡片暂存
   * （卡片编号 → File[]），老师在这张卡片上点“放弃并开始”后再归到新线程，点“继续”就丢掉。
   * 不能归到旧流程的线程，那会换掉它的文件；也不能共用一个槽位，否则别的会话、别的卡片会拿走它。
   */
  const stagedByCard = new Map();

  const timers = new Map();

  // --- 设置 --------------------------------------------------------------------

  async function loadSettings() {
    try {
      settings.value = await api.get("/assistant/settings");
      let dismissed = false;
      try { dismissed = window.localStorage.getItem(SETUP_DISMISSED_KEY) === "1"; } catch { /* 隐私模式 */ }
      // 第一次使用时请用户配置（U9）；关掉就按默认顺序，不再反复打扰。开发模式没有可选项，不弹。
      const deployment = settings.value.effective?.source === "deployment";
      if (!settings.value.configured && !dismissed && !deployment) setupOpen.value = true;
    } catch (err) {
      if (!(err instanceof StaleContextError)) settings.value = null;
    }
  }

  async function saveSettings(input) {
    settings.value = await api.put("/assistant/settings", input);
    setupOpen.value = false;
  }

  function dismissSetup() {
    try { window.localStorage.setItem(SETUP_DISMISSED_KEY, "1"); } catch { /* 隐私模式 */ }
    setupOpen.value = false;
  }

  // --- 会话 --------------------------------------------------------------------

  async function loadConversations() {
    try {
      conversations.value = (await api.get("/assistant/conversations")) || [];
    } catch (err) {
      if (!(err instanceof StaleContextError)) conversations.value = [];
    }
  }

  function stopWatchers() {
    for (const timer of timers.values()) window.clearTimeout(timer);
    timers.clear();
  }

  function clearConversation() {
    stopWatchers();
    conversation.value = null;
    messages.value = [];
    pending.value = null;
    workspacePath.value = null;
    error.value = null;
    resetWorkspace();
  }

  // --- 工作区 --------------------------------------------------------------------

  function resetWorkspace() {
    ws.tab = "task";
    ws.step = null;
    ws.toast = null;
    ws.lastTaskEdit = 0;
  }

  /** 打开任务标签的某一步（null 为当前步骤）；工作区收起时展开。 */
  function openStep(step = null) {
    ws.tab = "task";
    ws.step = step;
    if (ws.toast === "task") ws.toast = null;
    if (ws.mode === "closed") ws.mode = "open";
  }

  function openArtifact(id) {
    ws.tab = id;
    ws.toast = null;
    if (ws.mode === "closed") ws.mode = "open";
  }

  /** 老师在任务标签上操作过（确认细则、选文件……）：一分钟内的新结果改为提示，不打断。 */
  function noteTaskEdit() {
    ws.lastTaskEdit = Date.now();
  }

  /**
   * 新出现的结果卡片：打开它的标签，或在老师正忙时只提示一下；新流程开始（计划卡片）时打开任务标签。
   * 窄屏工作区收着时不弹出来，盖住对话反而打断老师。
   */
  function revealArtifacts(incoming) {
    const cards = [...(incoming || [])].reverse().flatMap((item) => [...(item.cards || [])].reverse());
    if (cards.some((item) => item.type === "plan")) {
      if (!(ws.mode === "closed" && ws.narrow)) openStep(null);
      return;
    }
    const card = cards.find((item) => ARTIFACT_TYPES.has(item.type) && !item.step);
    if (!card) return;
    const busy = ws.mode !== "closed" && ws.tab === "task" && Date.now() - ws.lastTaskEdit < BUSY_MS;
    if (busy) {
      ws.toast = card.id;
      return;
    }
    if (ws.mode === "closed" && ws.narrow) return;
    openArtifact(card.id);
  }

  function applyConversation(next) {
    const previousRev = conversation.value?.focus?.workspace_rev;
    const changedConversation = conversation.value?.id !== next.id;
    const { messages: _messages, pending: _pending, ...summary } = next;
    conversation.value = summary;
    const focus = summary.focus || {};
    if (focus.workspace && (changedConversation || focus.workspace_rev !== previousRev)) {
      workspacePath.value = focus.workspace;
      workspaceKey.value += 1;
    }
    conversations.value = [summary, ...conversations.value.filter((item) => item.id !== summary.id)];
  }

  async function open(conversationId) {
    stopWatchers();
    error.value = null;
    const detail = await api.get(`/assistant/conversations/${conversationId}`);
    messages.value = detail.messages || [];
    pending.value = detail.pending || null;
    conversation.value = null;
    applyConversation(detail);
    if (!detail.focus?.workspace) workspacePath.value = null;
    resetWorkspace();
    reactToPending();
    return detail;
  }

  async function create() {
    const created = await api.post("/assistant/conversations", {});
    messages.value = [];
    pending.value = null;
    applyConversation(created);
    return created;
  }

  async function remove(conversationId) {
    await api.del(`/assistant/conversations/${conversationId}`);
    conversations.value = conversations.value.filter((item) => item.id !== conversationId);
    if (conversation.value?.id === conversationId) clearConversation();
  }

  function showWorkspace(path) {
    workspacePath.value = path;
    workspaceKey.value += 1;
  }

  // --- 运行 --------------------------------------------------------------------

  function mergeMessages(incoming) {
    const byId = new Map(messages.value.map((item) => [item.id, item]));
    for (const message of incoming || []) byId.set(message.id, message);
    messages.value = [...byId.values()].sort((a, b) =>
      String(a.created_at).localeCompare(String(b.created_at)) || String(a.id).localeCompare(String(b.id)));
  }

  async function run(body) {
    const target = conversation.value || (await create());
    stopWatchers();
    const result = await api.post(`/assistant/conversations/${target.id}/runs`, body);
    const known = new Set(messages.value.map((item) => item.id));
    mergeMessages(result.messages);
    revealArtifacts((result.messages || []).filter((item) => !known.has(item.id)));
    pending.value = result.pending || null;
    applyConversation(result.conversation);
    return result;
  }

  /** 串行执行：同一会话同一时间只发一个运行请求（后端也有运行锁）。 */
  async function guarded(fn, fallback) {
    if (sending.value) return undefined;
    sending.value = true;
    error.value = null;
    let result;
    try {
      result = await fn();
    } catch (err) {
      if (!(err instanceof StaleContextError)) error.value = errorText(err, fallback);
    } finally {
      sending.value = false;
    }
    reactToPending();
    return result;
  }

  /**
   * 发一句话。快捷按钮带上 `quickAction`（意图键），后端按键直接分派，不经规则与模型。
   * @param {string} text
   * @param {string} [quickAction]
   */
  function send(text, quickAction) {
    const cleaned = (text || "").trim();
    if (!cleaned) return Promise.resolve(undefined);
    const body = quickAction ? { type: "message", text: cleaned, quick_action: quickAction } : { type: "message", text: cleaned };
    return guarded(() => run(body), "发送失败");
  }

  function resume(cardId, value) {
    return guarded(() => run({ type: "resume", card_id: cardId, value }), "操作失败");
  }

  function select(cardId, value) {
    return guarded(() => run({ type: "select", card_id: cardId, value }), "操作失败");
  }

  /** 工作区底部与任务条上的“开始评分”“不评了”：恢复正在等确认的流程（与卡片上的按钮同一个动作）。 */
  function answerConfirm(action) {
    const current = pending.value;
    if (current?.kind !== "confirm_start") return Promise.resolve(undefined);
    noteTaskEdit();
    return resume(current.card_id, { action });
  }

  /** 选文件之前换评分标准（v2 界面改造方案 §4.3）。 */
  function changeRubric(rubricId) {
    const current = pending.value;
    if (current?.kind !== "pick_files") return Promise.resolve(undefined);
    return resume(current.card_id, { action: "change_rubric", rubric_id: rubricId });
  }

  /** 卡片当前是否就是流程在等的那一张。 */
  function isPending(card) {
    return Boolean(pending.value && pending.value.card_id === card.id);
  }

  function filesForPending() {
    const thread = pending.value?.thread_id;
    return thread ? filesByThread[thread] || null : null;
  }

  // --- 选文件与上传（T5：文件由浏览器直传，不经过助手接口） ---------------------------------------

  /** 输入框旁的附件按钮：等着选文件就交给那张卡片，否则直接进入关键确认。 */
  async function attachFiles(fileList) {
    const { accepted } = limitFiles(fileList);
    if (!accepted.length) return;
    if (pending.value?.kind === "pick_files") {
      await pickFiles({ id: pending.value.card_id }, accepted);
      return;
    }
    const names = accepted.map((file) => file.name);
    const before = pending.value?.thread_id || null;
    const result = await guarded(
      () => run({ type: "message", attachments: { count: accepted.length, names } }),
      "处理文件失败",
    );
    const thread = result?.pending?.thread_id;
    if (thread && thread !== before) {
      filesByThread[thread] = accepted;
      return;
    }
    const card = proposedSwitchCard(result); // 后端先问要不要放弃上一步
    if (card) stagedByCard.set(card.id, accepted);
  }

  function proposedSwitchCard(result) {
    for (const item of result?.messages || []) {
      const card = (item.cards || []).find((entry) => entry.type === "confirm_switch_flow" && entry.status === "proposed");
      if (card) return card;
    }
    return null;
  }

  /**
   * “要放弃上一步吗”的确认卡片：放弃并开始新流程，或继续原来那一步。
   * @param {{id: string}} card
   * @param {"switch"|"keep"} action
   */
  async function switchFlow(card, action) {
    const before = pending.value?.thread_id || null;
    const result = await select(card.id, { action });
    if (!result) return result; // 请求失败：文件留着，再点一次还在
    const staged = stagedByCard.get(card.id);
    stagedByCard.delete(card.id);
    const thread = result.pending?.thread_id;
    if (action === "switch" && staged && thread && thread !== before) filesByThread[thread] = staged;
    return result;
  }

  async function pickFiles(card, fileList) {
    const { accepted } = limitFiles(fileList);
    if (!accepted.length || !pending.value) return;
    filesByThread[pending.value.thread_id] = accepted;
    noteTaskEdit();
    await resume(card.id, { count: accepted.length, names: accepted.map((file) => file.name) });
  }

  /** 上传卡片在文件失效（刷新过页面）时，让用户重新选择同一批文件。 */
  async function replaceFiles(fileList) {
    const { accepted } = limitFiles(fileList);
    if (!accepted.length || !pending.value) return;
    filesByThread[pending.value.thread_id] = accepted;
    await uploadPending();
  }

  async function uploadPending() {
    const current = pending.value;
    if (current?.kind !== "upload_papers" || uploading.value) return;
    const files = filesForPending();
    if (!files) return; // 卡片提示重新选择文件
    const upload = useUploadStore();
    uploading.value = true;
    let paperIds = null;
    try {
      upload.reset();
      await upload.loadCapabilities();
      upload.stage(files);
      await upload.uploadAll(current.client.batch_id);
      // 已归档的材料都交给后端：解析失败的由预检发现，再由用户决定是否移除（B1）。
      paperIds = [...upload.uploadedPaperIds].slice(0, MAX_FILES);
    } catch (err) {
      if (!(err instanceof StaleContextError)) error.value = errorText(err, "上传失败");
    } finally {
      uploading.value = false;
    }
    if (paperIds) await resume(current.card_id, { paper_ids: paperIds });
  }

  // --- 场景 B：建评分标准导入草稿（与评分标准页同一个动作） ------------------------------------------

  async function importRubric(card, input) {
    const rubrics = useRubricsStore();
    let sessionId = null;
    try {
      const session = await rubrics.createImportSession({
        name: input.name, version: input.version || "v1.0",
        businessProfileKey: input.businessProfileKey,
        rulesFile: input.rulesFile, templateFile: input.templateFile,
      });
      rememberDocumentType(input.businessProfileKey);
      sessionId = session.id;
    } catch (err) {
      reportError(err, "导入失败");
      return;
    }
    await resume(card.id, { action: "created", import_session_id: sessionId });
  }

  // --- 轮询：只负责发现“可能结束了”，真正是否结束由后端核对 ------------------------------------------

  function schedule(key, fn, delay) {
    window.clearTimeout(timers.get(key));
    timers.set(key, window.setTimeout(fn, delay));
  }

  function watchJob(current) {
    const batchId = current.client.batch_id;
    const tick = async () => {
      if (pending.value?.card_id !== current.card_id) return undefined;
      if (document.visibilityState === "hidden") return schedule("job", tick, JOB_POLL_MS);
      try {
        const job = await api.get(`/batches/${batchId}/score-jobs/latest`);
        jobs[batchId] = job;
        if (job && TERMINAL_JOB_STATUSES.has(job.status)) {
          timers.delete("job");
          await resume(current.card_id, { event: "job_finished" });
          return undefined;
        }
      } catch (err) {
        if (err instanceof StaleContextError) return undefined;
      }
      return schedule("job", tick, JOB_POLL_MS);
    };
    schedule("job", tick, 0);
  }

  function watchRubric(current) {
    const sessionId = current.client.import_session_id;
    const tick = async () => {
      if (pending.value?.card_id !== current.card_id) return undefined;
      if (document.visibilityState === "hidden") return schedule("rubric", tick, RUBRIC_POLL_MS);
      let ready = false;
      try {
        const session = await api.get(`/rubrics/import-sessions/${sessionId}`);
        if (["expired", "cancelled"].includes(session.status)) ready = true;
        if (session.status === "confirmed" && session.rubric_id) {
          const rubric = await api.get(`/rubrics/${session.rubric_id}`);
          ready = rubric.status === "published";
        }
      } catch (err) {
        if (err instanceof StaleContextError) return undefined;
        ready = err instanceof ApiError && err.status === 410;
      }
      if (ready) {
        timers.delete("rubric");
        await resume(current.card_id, { action: "check" });
        return undefined;
      }
      return schedule("rubric", tick, RUBRIC_POLL_MS);
    };
    schedule("rubric", tick, 0);
  }

  function reactToPending() {
    const current = pending.value;
    if (!current || sending.value) return;
    const type = current.client?.type;
    if (type === "watch_job") watchJob(current);
    if (type === "watch_rubric") watchRubric(current);
    if (type === "upload_papers") uploadPending();
  }

  /** 卡片按钮的异常统一显示在输入框上方。 */
  function reportError(err, fallback = "操作失败") {
    if (!(err instanceof StaleContextError)) error.value = errorText(err, fallback);
  }

  function reset() {
    clearConversation();
    conversations.value = [];
    settings.value = null;
    for (const key of Object.keys(filesByThread)) delete filesByThread[key];
    stagedByCard.clear();
  }

  // 切走会话时停止轮询，避免旧会话的轮询去恢复新会话的流程。
  watch(() => conversation.value?.id, (next, previous) => {
    if (previous && next !== previous) stopWatchers();
  });

  return {
    settings, conversations, conversation, messages, pending, sending, uploading, error, setupOpen,
    workspacePath, workspaceKey, jobs, filesByThread, ws,
    loadSettings, saveSettings, dismissSetup, loadConversations, open, create, remove, clearConversation,
    showWorkspace, send, resume, select, switchFlow, isPending, filesForPending, attachFiles, pickFiles, replaceFiles,
    importRubric, reportError, reset, stopWatchers,
    openStep, openArtifact, noteTaskEdit, answerConfirm, changeRubric,
  };
});
