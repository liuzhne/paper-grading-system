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
/** 流式断线后取回结果：每隔这么久读一次会话，最多等这么久（回合上限 60 秒加余量，过程流式方案 §5）。 */
const RECOVER_POLL_MS = 2000;
const RECOVER_WAIT_MS = 70000;
const RECOVERING = "连接中断，正在取回结果…";
const CHECKING = "出了点问题，正在核对这句话有没有发出去…";

/**
 * 确认不了这句话有没有被收下：不能让老师重发（可能多一条），本地消息标“还不确定”。
 * 等不到这次运行结束、或者断线后会话里找不到它（请求可能还没到后端）时用。
 */
class DeliveryUnknownError extends ApiError {
  constructor(what = "这句话") {
    super(0, `还没确认${what}有没有发出去，请稍后刷新页面看看，先别重新发送。`, null);
    this.name = "DeliveryUnknownError";
  }
}

/**
 * 后端存下的那条老师消息的文字：说一句话就是这句话；附件是后端拼的“评分这 N 份文件”
 * （`api/routes/assistant._run_request`）。核对送达按文字对应，不按“会话里新出现了老师消息”：
 * 另一个标签页在同一会话里说的话也会新出现（#79 复审）。
 */
function sentText(body, local) {
  if (local) return local.text;
  if (body.attachments) return `评分这 ${body.attachments.count} 份文件`;
  return body.text || null;
}

/** 后端自己返回的错误（响应体是后端的 JSON，带 detail）：这次请求已经处理完。代理或网关的 502、504 不是。 */
function fromBackend(err) {
  return err instanceof ApiError && Boolean(err.payload) && typeof err.payload === "object" && "detail" in err.payload;
}

/**
 * 这句话有没有被后端收下说不准：5xx（后端先提交消息再返回，返回前出错也是 5xx）、请求发出后连接断了。
 * 4xx（运行锁 409、校验 422……）是确实没收下，可以直接重发。
 */
function deliveryUncertain(err) {
  if (err instanceof StaleContextError) return false;
  if (err instanceof StreamInterruptedError) return true;
  if (err instanceof ApiError) return err.status >= 500;
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
   */
  let localSeq = 0;
  function echo(text, quickAction) {
    localSeq += 1;
    const item = { id: `local-${localSeq}`, role: "user", text, cards: [], created_at: new Date().toISOString(),
      local: "sending", quick_action: quickAction || null };
    messages.value = [...messages.value, item];
    return item;
  }

  function dropLocal(id) {
    messages.value = messages.value.filter((item) => item.id !== id);
  }

  /** 本地消息的状态：`failed` 没有发出去，可以重新发送；`unknown` 还不确定有没有发出去，不给重发。 */
  function markLocal(id, state) {
    messages.value = messages.value.map((item) => (item.id === id ? { ...item, local: state } : item));
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
    const known = new Set(messages.value.map((item) => item.id));
    let result;
    try {
      // 说一句话走流式（开关打开时），回合进行时能看到进度；卡片上的“确定”“选择”不调模型，照旧。
      result = body.type === "message" && settings.value?.stream_enabled
        ? await runStream(target.id, body)
        : await api.post(`/assistant/conversations/${target.id}/runs`, body);
    } catch (err) {
      // 说一句话时结果说不准（5xx、连接断了）：先核对会话，不让老师直接重发。卡片上的操作照旧报错。
      if (body.type !== "message" || !deliveryUncertain(err)) throw err;
      result = await recoverRun(target.id, sentText(body, local), local ? "这句话" : "这批文件", known, err);
    }
    if (local) dropLocal(local.id);
    const before = new Set(messages.value.map((item) => item.id));
    mergeMessages(result.messages);
    revealArtifacts((result.messages || []).filter((item) => !before.has(item.id)));
    pending.value = result.pending || null;
    applyConversation(result.conversation);
    return result;
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

  /**
   * 这句话有没有被后端收下说不准时，不重发（会多一条），先核对会话。会话里的消息和这次请求只能按文字对应：
   * - 后端自己返回的 5xx：这次请求已经处理完，读一次会话（不等 `running`）。有这句话就照常显示；没有就是没收下，
   *   报后端的原因、标“没有发出去”。
   * - 代理或网关的 5xx、请求发出后连接断了：等会话的 `running` 变成 false 再读，最多 70 秒。有这句话就照常显示；
   *   没有也不能说没收下——请求可能还没到后端、还没拿到运行锁（#79 复审），标“还不确定”，不给重发。
   * `running` 只看运行锁，锁过期不会停后台线程；回合的模型请求受 60 秒截止时间约束，实际在锁过期前结束。
   * 另一个标签页恰好在同一会话说了同样的话时会对错，这种情况不处理（要按请求编号去重才能根治）。
   */
  async function recoverRun(conversationId, text, what, known, cause) {
    const finished = fromBackend(cause);
    progress.value = { label: finished ? CHECKING : RECOVERING, since: Date.now() };
    const deadline = Date.now() + RECOVER_WAIT_MS;
    try {
      for (;;) {
        let detail = null;
        try {
          detail = await api.get(`/assistant/conversations/${conversationId}`);
        } catch (err) {
          if (err instanceof StaleContextError) throw err;
        }
        if (detail && (finished || !detail.running)) {
          const fresh = (detail.messages || []).filter((item) => !known.has(item.id));
          const delivered = text !== null && fresh.some((item) => item.role === "user" && item.text === text);
          if (delivered) return { messages: detail.messages || [], pending: detail.pending || null, conversation: detail };
          if (finished) throw cause; // 请求处理完了，会话里没有：确实没收下
          throw new DeliveryUnknownError(what);
        }
        if (Date.now() >= deadline) throw new DeliveryUnknownError(what);
        await new Promise((resolve) => { window.setTimeout(resolve, RECOVER_POLL_MS); });
      }
    } finally {
      progress.value = null;
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
  async function send(text, quickAction) {
    const cleaned = (text || "").trim();
    if (!cleaned || sending.value) return undefined;
    const body = quickAction ? { type: "message", text: cleaned, quick_action: quickAction } : { type: "message", text: cleaned };
    const local = echo(cleaned, quickAction);
    let failure = null;
    const result = await guarded(async () => {
      try {
        return await run(body, local);
      } catch (err) {
        failure = err;
        throw err;
      }
    }, "发送失败");
    if (result === undefined) markLocal(local.id, failure instanceof DeliveryUnknownError ? "unknown" : "failed");
    return result;
  }

  /** 重新发送一句没发出去的话：去掉那条本地消息，按原样再发一次（快捷按钮仍带意图键）。 */
  function resend(localId) {
    const item = messages.value.find((entry) => entry.id === localId && entry.local === "failed");
    if (!item || sending.value) return Promise.resolve(undefined);
    dropLocal(item.id);
    return send(item.text, item.quick_action || undefined);
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
    settings, conversations, conversation, messages, pending, sending, progress, uploading, error, setupOpen,
    workspacePath, workspaceKey, jobs, filesByThread, ws,
    loadSettings, saveSettings, dismissSetup, loadConversations, open, create, remove, clearConversation,
    showWorkspace, send, resend, resume, select, switchFlow, isPending, filesForPending, attachFiles, pickFiles, replaceFiles,
    importRubric, reportError, reset, stopWatchers,
    openStep, openArtifact, noteTaskEdit, answerConfirm, changeRubric,
  };
});
