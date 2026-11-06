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

import { api, ApiError, postStream, StaleContextError, StreamInterruptedError } from "@/api/client.js";
import { MAX_FILES, TERMINAL_JOB_STATUSES, limitFiles } from "@/lib/assistant-flow.js";
import { rememberDocumentType } from "@/lib/document-types.js";
import { useRubricsStore } from "@/stores/rubrics.js";
import { useUploadStore } from "@/stores/upload.js";

const JOB_POLL_MS = 3000;
const RUBRIC_POLL_MS = 5000;
/** 发一句话没有确定结果时，等这次运行结束再用同一个编号重发：每隔这么久读一次会话，最多等这么久（回合上限 60 秒加余量）。 */
const RETRY_POLL_MS = 2000;
const RETRY_WAIT_MS = 70000;
const RETRYING = "连接出了问题，正在重新发送…";
const WAITING_TURN = "上一步还在处理，处理完就自动发出…";

/**
 * 请求编号（发送去重，docs/评分助手发送去重方案.md）：后端按它去重，同一个编号重发不会多一条，回放第一次的结果。
 * 非安全上下文没有 `randomUUID`，用 `getRandomValues` 拼一个同样格式的。
 */
export function makeClientId() {
  const source = globalThis.crypto;
  if (typeof source?.randomUUID === "function") return source.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof source?.getRandomValues === "function") source.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/**
 * 发一句话没有确定结果、值得用同一个编号重发一次的情况：5xx（后端先提交消息再返回，返回前出错也是 5xx）、
 * 请求发出后连接断了、409（运行锁被占，可能就是自己第一次的请求还在跑）。其余 4xx 是确实没收下。
 */
function worthRetry(err) {
  if (err instanceof StaleContextError) return false;
  if (err instanceof StreamInterruptedError) return true;
  if (err instanceof ApiError) return err.status >= 500 || err.status === 409;
  return true; // fetch 本身失败（网络中断）：请求可能已经到了后端
}
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
  /** 过程流式：回合进行时“现在在做哪一步”`{label, since}`，没有时为 null（docs/评分助手过程流式方案.md §3）。 */
  const progress = ref(/** @type {{label: string, since: number} | null} */ (null));
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

  /**
   * 老师这句话先显示出来，不等后端返回（本地消息，编号以 `local-` 开头）。返回结果里带着后端存下的那一条，
   * 收到后换掉；没发出去就留着并标“没有发出去”，老师可以重新发送，输入框已经清空了，不能让这句话丢掉。
   * `client_id` 是请求编号：重新发送沿用它，后端按它去重。
   */
  let localSeq = 0;
  function echo(text, quickAction, clientId) {
    localSeq += 1;
    const item = { id: `local-${localSeq}`, role: "user", text, cards: [], created_at: new Date().toISOString(),
      local: "sending", quick_action: quickAction || null, client_id: clientId };
    messages.value = [...messages.value, item];
    return item;
  }

  function dropLocal(id) {
    messages.value = messages.value.filter((item) => item.id !== id);
  }

  /** 没有发出去：留着这句话，给“重新发送”（沿用同一个编号，后端去重，不会多一条）。 */
  function markFailed(id) {
    messages.value = messages.value.map((item) => (item.id === id ? { ...item, local: "failed" } : item));
  }

  function mergeMessages(incoming) {
    const byId = new Map(messages.value.map((item) => [item.id, item]));
    for (const message of incoming || []) byId.set(message.id, message);
    messages.value = [...byId.values()].sort((a, b) =>
      String(a.created_at).localeCompare(String(b.created_at)) || String(a.id).localeCompare(String(b.id)));
  }

  async function run(body, local = null) {
    const target = conversation.value || (await create());
    // 新会话：create() 清空了消息列表，把刚显示的这句话放回去。
    if (local && !messages.value.some((item) => item.id === local.id)) messages.value = [...messages.value, local];
    stopWatchers();
    let result;
    try {
      result = await dispatch(target.id, body);
    } catch (err) {
      // 说一句话没有确定结果：等这次运行结束，用同一个编号重发一次——后端收到过就回放第一次的结果，
      // 没收到就照常处理，不会多一条（发送去重方案 §4.2）。重发也失败就交给调用方标“没有发出去”。
      if (body.type !== "message" || !body.client_id || !worthRetry(err)) throw err;
      result = { ...(await retryOnce(target.id, body, err)), retried: true };
    }
    if (local) dropLocal(local.id);
    const before = new Set(messages.value.map((item) => item.id));
    mergeMessages(result.messages);
    revealArtifacts((result.messages || []).filter((item) => !before.has(item.id)));
    pending.value = result.pending || null;
    applyConversation(result.conversation);
    return result;
  }

  /** 说一句话走流式（开关打开时），回合进行时能看到进度；卡片上的“确定”“选择”不调模型，照旧。 */
  function dispatch(conversationId, body) {
    return body.type === "message" && settings.value?.stream_enabled
      ? runStream(conversationId, body)
      : api.post(`/assistant/conversations/${conversationId}/runs`, body);
  }

  async function runStream(conversationId, body) {
    progress.value = null;
    try {
      return await postStream(`/assistant/conversations/${conversationId}/runs/stream`, body, {
        onEvent(event, data) {
          if (event === "progress" && typeof data?.label === "string") progress.value = { label: data.label, since: Date.now() };
        },
      });
    } finally {
      progress.value = null;
    }
  }

  async function retryOnce(conversationId, body, cause) {
    const busy = cause instanceof ApiError && cause.status === 409;
    progress.value = { label: busy ? WAITING_TURN : RETRYING, since: Date.now() };
    try {
      await waitUntilIdle(conversationId);
      return await dispatch(conversationId, body);
    } finally {
      progress.value = null;
    }
  }

  /**
   * 等会话的运行结束（`running` 变成 false），最多 70 秒；读不到会话就接着等。到时间就不等了，直接重发：
   * 还在跑会是 409，重发失败，标“没有发出去”，老师之后再点“重新发送”也是同一个编号，不会多一条。
   */
  async function waitUntilIdle(conversationId) {
    const deadline = Date.now() + RETRY_WAIT_MS;
    for (;;) {
      try {
        const detail = await api.get(`/assistant/conversations/${conversationId}`);
        if (!detail?.running) return;
      } catch (err) {
        if (err instanceof StaleContextError) throw err;
      }
      if (Date.now() >= deadline) return;
      await new Promise((resolve) => { window.setTimeout(resolve, RETRY_POLL_MS); });
    }
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
  async function send(text, quickAction, clientId = makeClientId()) {
    const cleaned = (text || "").trim();
    if (!cleaned || sending.value) return undefined;
    const body = { type: "message", text: cleaned, client_id: clientId };
    if (quickAction) body.quick_action = quickAction;
    const local = echo(cleaned, quickAction, clientId);
    const result = await guarded(() => run(body, local), "发送失败");
    if (result === undefined) markFailed(local.id);
    return result;
  }

  /** 重新发送一句没发出去的话：去掉那条本地消息，按原样、用同一个编号再发一次（快捷按钮仍带意图键）。 */
  function resend(localId) {
    const item = messages.value.find((entry) => entry.id === localId && entry.local === "failed");
    if (!item || sending.value) return Promise.resolve(undefined);
    dropLocal(item.id);
    return send(item.text, item.quick_action || undefined, item.client_id || makeClientId());
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
      () => run({ type: "message", attachments: { count: accepted.length, names }, client_id: makeClientId() }),
      "处理文件失败",
    );
    const thread = result?.pending?.thread_id;
    // 重发拿到的可能是回放：只有等待中的卡片是这一轮出的，才把文件交给它的流程（另一个标签页可能开了别的流程）。
    if (thread && thread !== before && (!result.retried || ownsPending(result))) {
      filesByThread[thread] = accepted;
      return;
    }
    const card = proposedSwitchCard(result); // 后端先问要不要放弃上一步
    if (card) stagedByCard.set(card.id, accepted);
  }

  /** 当前等待的卡片是不是这一轮（返回的消息）里出的。 */
  function ownsPending(result) {
    const cardId = result?.pending?.card_id;
    return Boolean(cardId) && (result.messages || []).some((item) => (item.cards || []).some((card) => card.id === cardId));
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
    settings, conversations, conversation, messages, pending, sending, progress, uploading, error, setupOpen,
    workspacePath, workspaceKey, jobs, filesByThread, ws,
    loadSettings, saveSettings, dismissSetup, loadConversations, open, create, remove, clearConversation,
    showWorkspace, send, resend, resume, select, switchFlow, isPending, filesForPending, attachFiles, pickFiles, replaceFiles,
    importRubric, reportError, reset, stopWatchers,
    openStep, openArtifact, noteTaskEdit, answerConfirm, changeRubric,
  };
});
