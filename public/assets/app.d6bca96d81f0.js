const defaultCriteria = [
  {
    code: "C01",
    name: "选题意义",
    max_score: 10,
    description: "考察选题的理论意义、现实意义和问题价值。",
    evidence_hints: ["绪论", "研究背景", "研究意义"],
    deduction_rules: ["研究意义表述笼统，扣 1 到 3 分"],
    display_order: 1,
  },
  {
    code: "C02",
    name: "文献综述",
    max_score: 15,
    description: "考察文献覆盖、归纳能力和研究空白识别。",
    evidence_hints: ["文献综述", "国内外研究现状", "相关工作"],
    deduction_rules: ["文献覆盖不足，扣 2 到 5 分"],
    display_order: 2,
  },
  {
    code: "C03",
    name: "研究方法",
    max_score: 20,
    description: "考察方法合理性、实验设计和数据来源。",
    evidence_hints: ["研究方法", "实验设计", "数据来源"],
    deduction_rules: ["方法说明不清晰，扣 2 到 6 分"],
    display_order: 3,
  },
  {
    code: "C04",
    name: "论文创新性",
    max_score: 15,
    description: "考察创新点、对比现有研究和贡献。",
    evidence_hints: ["创新点", "贡献", "改进"],
    deduction_rules: ["创新性不足，扣 2 到 5 分"],
    display_order: 4,
  },
  {
    code: "C05",
    name: "论证与分析",
    max_score: 20,
    description: "考察逻辑严密性、数据分析和结论支撑。",
    evidence_hints: ["实验结果", "结果分析", "讨论"],
    deduction_rules: ["论证链条不完整，扣 2 到 6 分"],
    display_order: 5,
  },
  {
    code: "C06",
    name: "写作规范",
    max_score: 10,
    description: "考察结构完整、格式、图表和语言规范。",
    evidence_hints: ["摘要", "关键词", "目录", "结论"],
    deduction_rules: ["结构或格式缺项，扣 1 到 4 分"],
    display_order: 6,
  },
  {
    code: "C07",
    name: "参考文献",
    max_score: 10,
    description: "考察参考文献数量、格式、引用规范和时效性。",
    evidence_hints: ["参考文献", "引用"],
    deduction_rules: ["参考文献数量或格式不足，扣 1 到 4 分"],
    display_order: 7,
  },
].map((item) => ({
  ...item,
  criterion_type: "llm_judgment",
  scoring_mode: "deductive",
  deduction_rules_structured: [],
}));

const state = {
  page: "dashboard",
  rubrics: [],
  batches: [],
  papers: [],
  runs: [],
  integrations: null,
  selectedRubricId: "",
  selectedLifecycleRubricId: "",
  rubricLifecycle: null,
  rubricImportPreview: null,
  selectedBatchId: "",
  batchScoreJob: null,
  selectedPaperId: "",
  selectedRunId: "",
  chunksById: {},
  exportLogs: [],
  ranking: null,
  drift: null,
  anchors: [],
  anchorRubricId: "",
  authRequired: false,
  identity: null,
  organizations: [],
  currentOrganizationId: "",
  organizationMembers: [],
  latestInvitationLink: "",
  latestPasswordReset: null,
  aiConnections: [],
  rubricVisibilityFilter: "all",
  llmStatus: { phase: "idle" },
  llmTimer: null,
  newCriteriaDraft: JSON.parse(JSON.stringify(defaultCriteria)),
  editCriteriaDraft: [],
  editCriteriaRubricId: "",
  rubricEditStatus: "clean",
  rubricEditError: null,
  aiRuleDrafts: {},
  paperUploadQueue: [],
  paperUploadRunning: false,
};

const pageMeta = {
  dashboard: ["工作台", "从模板开始，继续最近的评分工作。"],
  rubrics: ["模板中心", "创建、调整并发布可复用的评分模板。"],
  batches: ["评分任务", "绑定模板、上传待评材料并运行批量评分。"],
  review: ["结果复核", "查看结论与证据，处理需要人工确认的评分项。"],
  exports: ["输出中心", "将评分结果导出为汇总表、评审报告或在线表格。"],
  settings: ["账户设置", "管理组织、成员和私有 AI 连接。"],
};

function apiBase() {
  const configured = window.__PGS_CONFIG__?.apiBase;
  return typeof configured === "string" ? configured.replace(/\/$/, "") : "/api";
}

const authPagePaths = {
  login: "/login",
  register: "/register",
  reset: "/reset-password",
};

function currentAuthPage() {
  return Object.entries(authPagePaths).find(([, path]) => window.location.pathname === path)?.[0] || null;
}

function setAuthError(elementId, message) {
  const error = document.querySelector(elementId);
  if (!error) return;
  error.textContent = message || "";
  error.classList.toggle("hidden", !message);
}

function showAuthPage(page, { message = "", updateUrl = true } = {}) {
  const shell = document.querySelector("#auth-shell");
  if (!shell) return;
  const pageId = `#auth-${page === "reset" ? "reset-password" : page}-page`;
  shell.classList.remove("hidden");
  shell.querySelectorAll(".auth-page").forEach((element) => element.classList.toggle("hidden", `#${element.id}` !== pageId));
  if (updateUrl && window.location.pathname !== authPagePaths[page]) {
    window.history.replaceState(null, "", authPagePaths[page]);
  }
  if (page === "login") {
    setAuthError("#login-error", message);
    const password = document.querySelector("#login-password");
    if (password) password.value = "";
  }
}

function showLogin(message) {
  showAuthPage("login", { message });
}

function hideLogin() {
  const shell = document.querySelector("#auth-shell");
  if (shell) shell.classList.add("hidden");
  if (currentAuthPage()) window.history.replaceState(null, "", "/");
}

function takeFragmentToken(name) {
  const token = new URLSearchParams(window.location.hash.slice(1)).get(name);
  if (token) window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  return token || "";
}

function setLogoutVisible(visible) {
  const btn = document.querySelector("#logout-btn");
  const trigger = document.querySelector("#account-menu-trigger");
  if (btn) btn.classList.toggle("hidden", !visible);
  if (trigger) trigger.disabled = !visible;
  if (!visible) closeAccountMenu();
}

function closeAccountMenu() {
  const menu = document.querySelector("#account-menu");
  const trigger = document.querySelector("#account-menu-trigger");
  if (menu) menu.classList.add("hidden");
  if (trigger) trigger.setAttribute("aria-expanded", "false");
}

function toggleAccountMenu() {
  const menu = document.querySelector("#account-menu");
  const trigger = document.querySelector("#account-menu-trigger");
  if (!menu || !trigger || trigger.disabled) return;
  const opening = menu.classList.contains("hidden");
  menu.classList.toggle("hidden", !opening);
  trigger.setAttribute("aria-expanded", String(opening));
}

function setConnectionStatus(status) {
  const panel = document.querySelector("#connection-status");
  const label = document.querySelector("#connection-status-label");
  const copy = {
    checking: "正在检查服务",
    healthy: "服务正常",
    failed: "连接失败",
  };
  if (panel) panel.dataset.state = status;
  if (label) label.textContent = copy[status] || copy.checking;
}

function setSidebarUser(user, { localMode = false, organizationName = "" } = {}) {
  const username = typeof user === "string" ? user.trim() : (user?.display_name || user?.username || "").trim();
  const name = document.querySelector("#sidebar-user-name");
  const context = document.querySelector("#sidebar-user-context");
  const avatar = document.querySelector("#sidebar-user-avatar");
  const displayName = username || (localMode ? "本地模式" : "访客");

  if (name) name.textContent = displayName;
  if (context) context.textContent = username ? (organizationName || "已登录") : (localMode ? "未启用登录" : "请登录");
  if (avatar) avatar.textContent = username ? Array.from(username)[0].toUpperCase() : "?";
}

async function refreshAuthState() {
  // 返回 true=可进入应用；false=需登录（已弹出登录框）。
  try {
    setConnectionStatus("checking");
    const statusResponse = await fetch(`${apiBase()}/auth/status`, { credentials: "same-origin" });
    if (!statusResponse.ok) throw new Error(`服务响应异常（${statusResponse.status}）`);
    const status = await statusResponse.json();
    setConnectionStatus("healthy");
    if (!status.auth_required) {
      state.authRequired = false;
      state.identity = null;
      state.organizations = [];
      state.currentOrganizationId = "";
      setLogoutVisible(false);
      setSidebarUser(null, { localMode: true });
      hideLogin();
      return true;
    }
    state.authRequired = true;
    const me = await fetch(`${apiBase()}/auth/me`, { credentials: "same-origin" });
    if (me.ok) {
      const identity = await me.json();
      state.identity = identity;
      state.currentOrganizationId = identity.organization?.id || "";
      // loadAll() will obtain the organization list with the other first-screen data.
      // Avoid an extra serialized database round trip immediately after login.
      state.organizations = [];
      setLogoutVisible(true);
      setSidebarUser(identity.user);
      hideLogin();
      return true;
    }
    state.identity = null;
    state.organizations = [];
    state.currentOrganizationId = "";
    setSidebarUser(null);
    if (!currentAuthPage()) showLogin();
    return false;
  } catch (_) {
    setConnectionStatus("failed");
    setSidebarUser(null);
    return true; // 连接失败仍展示页面，并在后续请求中重试。
  }
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  let response;
  try {
    response = await fetch(`${apiBase()}${path}`, Object.assign({}, options, { headers, credentials: "same-origin" }));
    setConnectionStatus(response.status < 500 ? "healthy" : "failed");
  } catch (_) {
    setConnectionStatus("failed");
    throw new Error("无法连接服务");
  }
  if (response.status === 401) {
    setLogoutVisible(false);
    setSidebarUser(null);
    showLogin("登录已失效，请重新登录");
    throw new Error("需要登录后重试");
  }
  if (!response.ok) {
    let detail = response.statusText;
    let problem = null;
    const text = await response.text();
    try {
      const payload = text ? JSON.parse(text) : {};
      detail = payload.detail || JSON.stringify(payload);
      if (detail && typeof detail === "object") problem = detail;
    } catch (_) {
      detail = text || response.statusText;
    }
    const error = new Error(problem?.message || String(detail));
    error.status = response.status;
    error.problem = problem;
    throw error;
  }
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) return response.json();
  return response;
}

const TUS_CHUNK_SIZE = 6 * 1024 * 1024;
const TUS_VERSION = "1.0.0";

function uploadError(message, kind = "http", status = 0) {
  const error = new Error(message);
  error.kind = kind;
  error.status = status;
  return error;
}

function directUploadMessage(error, fallback = "文件处理失败") {
  const message = error?.problem?.message || error?.message || fallback;
  const action = error?.problem?.user_action;
  return action ? `${message} ${action}` : message;
}

function uploadViaSignedUrl(file, intent, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", intent.signed_url, true);
    request.timeout = 120000;
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded, event.total);
    };
    request.onerror = () => reject(uploadError("网络连接中断", "network"));
    request.ontimeout = () => reject(uploadError("上传等待超时", "network"));
    request.onabort = () => reject(uploadError("上传已取消", "abort"));
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) resolve();
      else reject(uploadError(`私有存储拒绝上传（${request.status}）`, "http", request.status));
    };
    const body = new FormData();
    body.append("cacheControl", "3600");
    body.append("", file, file.name);
    request.send(body);
  });
}

function base64Metadata(value) {
  const bytes = new TextEncoder().encode(String(value));
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return window.btoa(binary);
}

function tusMetadata(intent, file) {
  const fields = {
    bucketName: intent.bucket_name,
    objectName: intent.object_path,
    contentType: file.type || "application/octet-stream",
    cacheControl: "3600",
  };
  return Object.entries(fields)
    .map(([name, value]) => `${name} ${base64Metadata(value)}`)
    .join(",");
}

function tusFingerprint(intent, file) {
  return `pgs:tus:${intent.paper.id}:${file.name}:${file.size}:${file.lastModified}`;
}

function savedTusUrl(key) {
  try { return window.sessionStorage.getItem(key) || ""; } catch (_) { return ""; }
}

function rememberTusUrl(key, value) {
  try {
    if (value) window.sessionStorage.setItem(key, value);
    else window.sessionStorage.removeItem(key);
  } catch (_) {
    // 隐私模式可能禁用会话存储；当前页面内仍可继续完成上传。
  }
}

async function tusRequest(url, options) {
  let response;
  try {
    response = await fetch(url, Object.assign({ credentials: "omit" }, options));
  } catch (_) {
    throw uploadError("网络连接中断", "network");
  }
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw uploadError(detail || `TUS 请求失败（${response.status}）`, "http", response.status);
  }
  return response;
}

function tusHeaders(intent, extra = {}) {
  return Object.assign({
    "Tus-Resumable": TUS_VERSION,
    "x-signature": intent.token,
  }, extra);
}

async function readTusOffset(uploadUrl, intent) {
  const response = await tusRequest(uploadUrl, {
    method: "HEAD",
    headers: tusHeaders(intent),
  });
  const offset = Number(response.headers.get("Upload-Offset"));
  if (!Number.isFinite(offset) || offset < 0) throw uploadError("TUS 返回了无效断点", "http");
  return offset;
}

async function createTusUpload(file, intent) {
  const response = await tusRequest(intent.tus_endpoint, {
    method: "POST",
    headers: tusHeaders(intent, {
      "Upload-Length": String(file.size),
      "Upload-Metadata": tusMetadata(intent, file),
      "x-upsert": "false",
    }),
  });
  const location = response.headers.get("Location");
  if (!location) throw uploadError("TUS 未返回可恢复地址", "http");
  return new URL(location, `${new URL(intent.tus_endpoint).origin}/`).href;
}

async function uploadViaTus(file, intent, onProgress) {
  const fingerprint = tusFingerprint(intent, file);
  let uploadUrl = savedTusUrl(fingerprint);
  let offset = 0;
  if (uploadUrl) {
    try {
      offset = await readTusOffset(uploadUrl, intent);
    } catch (error) {
      if (![404, 410].includes(error.status)) throw error;
      uploadUrl = "";
      rememberTusUrl(fingerprint, "");
    }
  }
  if (!uploadUrl) {
    uploadUrl = await createTusUpload(file, intent);
    rememberTusUrl(fingerprint, uploadUrl);
  }

  let retryCount = 0;
  while (offset < file.size) {
    const end = Math.min(offset + TUS_CHUNK_SIZE, file.size);
    try {
      const response = await tusRequest(uploadUrl, {
        method: "PATCH",
        headers: tusHeaders(intent, {
          "Upload-Offset": String(offset),
          "Content-Type": "application/offset+octet-stream",
        }),
        body: file.slice(offset, end),
      });
      const reported = Number(response.headers.get("Upload-Offset"));
      offset = Number.isFinite(reported) && reported >= end ? reported : end;
      retryCount = 0;
      onProgress(offset, file.size);
    } catch (error) {
      const recoverable = error.kind === "network" || error.status === 409 || error.status >= 500;
      if (!recoverable || retryCount >= 4) throw error;
      retryCount += 1;
      await new Promise((resolve) => window.setTimeout(resolve, [0, 1000, 3000, 5000][retryCount - 1]));
      offset = await readTusOffset(uploadUrl, intent);
      onProgress(offset, file.size);
    }
  }
  rememberTusUrl(fingerprint, "");
}

function updateUploadItem(item, patch) {
  Object.assign(item, patch);
  renderPaperUploadQueue();
}

async function confirmDirectUpload(item) {
  const paper = await api(`/papers/${item.intent.paper.id}/complete-upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ byte_size: item.file.size }),
  });
  item.archived = true;
  item.paperId = paper.id;
  return paper;
}

async function processUploadItem(item) {
  try {
    updateUploadItem(item, { status: "preparing", progress: 0, message: "正在申请安全上传凭证…" });
    if (!item.intent) {
      item.intent = await api("/papers/direct-upload-intents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          batch_id: item.batchId,
          file_name: item.file.name,
          content_type: item.file.type || "application/octet-stream",
          byte_size: item.file.size,
        }),
      });
      item.paperId = item.intent.paper.id;
    }
    item.mode = item.intent.mode;
    const onProgress = (loaded, total) => updateUploadItem(item, {
      status: "uploading",
      progress: total ? Math.min(100, Math.round((loaded / total) * 100)) : 0,
      message: item.mode === "tus" ? "正在断点续传到私有存储…" : "正在直传到私有存储…",
    });

    let alreadyArchived = item.archived;
    if (!item.transferComplete && item.mode === "standard") {
      try {
        await uploadViaSignedUrl(item.file, item.intent, onProgress);
        item.transferComplete = true;
      } catch (error) {
        if (error.kind !== "network") throw error;
        updateUploadItem(item, { mode: "tus", message: "检测到网络不稳定，已自动切换 TUS 断点续传…" });
        try {
          await confirmDirectUpload(item);
          alreadyArchived = true; // 标准上传可能已完成，只是浏览器未收到响应。
          item.transferComplete = true;
        } catch (confirmError) {
          const code = confirmError?.problem?.code;
          if (!["ARCHIVED_OBJECT_NOT_FOUND", "ARCHIVED_OBJECT_SIZE_MISMATCH"].includes(code)) throw confirmError;
        }
        if (!alreadyArchived) {
          await uploadViaTus(item.file, item.intent, onProgress);
          item.transferComplete = true;
        }
      }
    } else if (!item.transferComplete) {
      await uploadViaTus(item.file, item.intent, onProgress);
      item.transferComplete = true;
    }

    if (!alreadyArchived) {
      updateUploadItem(item, { status: "confirming", progress: 100, message: "正在确认私有桶归档完整性…" });
      await confirmDirectUpload(item);
    }
    updateUploadItem(item, { status: "parsing", message: "文件已归档，正在解析单份材料…" });
    const parsed = await api(`/papers/${item.paperId}/parse`, { method: "POST" });
    if (parsed.status !== "parsed") {
      const error = new Error(parsed.error_message || "材料解析失败");
      error.phase = "parse";
      throw error;
    }
    updateUploadItem(item, { status: "completed", progress: 100, message: "上传、归档和解析均已完成。" });
  } catch (error) {
    updateUploadItem(item, {
      status: item.archived ? "parse_failed" : item.transferComplete ? "confirm_failed" : "upload_failed",
      message: directUploadMessage(error),
    });
  }
}

async function runPaperUploads(files) {
  const additions = Array.from(files).map((file) => ({
    id: window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
    file,
    batchId: state.selectedBatchId,
    fileName: file.name,
    status: "queued",
    progress: 0,
    message: "等待上传",
    intent: null,
    archived: false,
    transferComplete: false,
  }));
  state.paperUploadQueue.push(...additions);
  state.paperUploadRunning = true;
  renderPaperUploadQueue();
  for (const item of additions) await processUploadItem(item);
  state.paperUploadRunning = false;
  await loadAll();
  const failed = additions.filter((item) => !["completed"].includes(item.status)).length;
  showToast(failed ? `${additions.length - failed} 个完成，${failed} 个需要处理` : `${additions.length} 个文件已归档并完成解析`, Boolean(failed));
}

function renderPaperUploadQueue() {
  const container = document.querySelector("#paper-upload-queue");
  if (!container) return;
  const button = document.querySelector("#upload-btn");
  if (button) button.disabled = state.paperUploadRunning;
  if (!state.paperUploadQueue.length) {
    container.innerHTML = '<div class="muted">文件将先直传私有存储，再逐份解析；失败项可单独恢复。</div>';
    return;
  }
  const visibleItems = state.paperUploadQueue.filter((item) => item.batchId === state.selectedBatchId);
  if (!visibleItems.length) {
    container.innerHTML = '<div class="muted">文件将先直传私有存储，再逐份解析；失败项可单独恢复。</div>';
    return;
  }
  container.innerHTML = visibleItems.map((item) => {
    const failed = ["upload_failed", "confirm_failed", "parse_failed"].includes(item.status);
    const retry = item.status === "upload_failed"
      ? `<button class="text-button" data-retry-upload="${escapeHtml(item.id)}">重试上传</button>`
      : item.status === "confirm_failed"
        ? `<button class="text-button" data-retry-upload="${escapeHtml(item.id)}">重试归档确认</button>`
      : item.status === "parse_failed" && item.paperId
        ? `<button class="text-button" data-retry-parse="${escapeHtml(item.paperId)}" data-upload-item="${escapeHtml(item.id)}">重新解析</button>`
        : "";
    const mode = item.mode ? (item.mode === "tus" ? "TUS 断点续传" : "标准签名直传") : "";
    return `<article class="paper-upload-item ${failed ? "error" : ""}">
      <div class="paper-upload-title"><strong>${escapeHtml(item.fileName)}</strong><span>${escapeHtml(mode)}</span></div>
      <div class="progress-track"><span style="width:${Number(item.progress || 0)}%"></span></div>
      <div class="paper-upload-detail"><span>${escapeHtml(item.message)}</span>${retry}</div>
    </article>`;
  }).join("");
}

function showToast(message, isError = false) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.className = `toast${isError ? " error" : ""}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 3600);
}

function renderRubricOperationError(error) {
  const problem = error?.problem || {
    code: "RUBRIC_OPERATION_FAILED",
    message: error?.message || "模板操作失败。",
    user_action: "请检查当前输入后重试。",
    retryable: false,
  };
  state.rubricEditError = problem;
  state.rubricEditStatus = "save_failed";
  renderRubricEditFeedback();
}

function renderRubricEditFeedback() {
  const status = document.querySelector("#rubric-edit-status");
  const error = document.querySelector("#rubric-edit-error");
  if (status) {
    const activeId = state.rubricLifecycle?.active_compilation?.id;
    const copy = {
      dirty: `当前修改尚未保存；第 3 步仍显示执行草稿 ${shortId(activeId) || "—"} 的旧校验结果。`,
      generating: "正在由 AI 起草缺失的扣分细则，当前输入会被保留。",
      ai_draft_pending: "AI 已生成建议，请确认后再保存并重新校验。",
      saving: "正在保存并生成新的执行草稿……",
      save_failed: "修改未保存；第 3 步仍显示上一次成功保存的校验结果。",
      clean: "",
    };
    status.textContent = copy[state.rubricEditStatus] || "";
    status.classList.toggle("hidden", !status.textContent);
  }
  if (error) {
    const problem = state.rubricEditError;
    error.innerHTML = problem
      ? `<strong>${escapeHtml(problem.message || "模板操作失败")}</strong><div>${escapeHtml(problem.user_action || "请检查当前输入后重试。")}</div>`
      : "";
    error.classList.toggle("hidden", !problem);
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function statusLabel(status) {
  return {
    draft: "草稿",
    review: "审核中",
    published: "已发布",
    active: "进行中",
    pending: "待处理",
    queued: "等待运行",
    running: "运行中",
    completed: "已完成",
    completed_with_errors: "部分完成",
    failed: "失败",
    uploading: "上传中",
    uploaded: "已归档",
    parsing: "解析中",
    canceled: "已取消",
    cancel_requested: "取消中",
    parsed: "已解析",
    scored: "已评分",
  }[status] || status || "未知";
}

function statusTone(status) {
  if (["published", "completed", "parsed", "scored", "approved", "confirmed", "validated"].includes(status)) return "ok";
  if (["failed", "rejected", "blocked"].includes(status)) return "error";
  if (["review", "running", "queued", "completed_with_errors", "cancel_requested"].includes(status)) return "warn";
  return "";
}

function optionHtml(items, valueField, labelFn, selectedValue = "") {
  return items
    .map((item) => {
      const value = item[valueField];
      const selected = value === selectedValue ? "selected" : "";
      return `<option value="${escapeHtml(value)}" ${selected}>${escapeHtml(labelFn(item))}</option>`;
    })
    .join("");
}

function renderTable(headers, rows) {
  if (!rows.length) return '<div class="muted">暂无数据</div>';
  return `<table><thead><tr>${headers.map((h) => `<th>${escapeHtml(h.label)}</th>`).join("")}</tr></thead><tbody>${rows
    .map((row) => `<tr>${headers.map((h) => `<td>${escapeHtml(h.value(row))}</td>`).join("")}</tr>`)
    .join("")}</tbody></table>`;
}

async function loadAll() {
  if (state.authRequired) {
    const canManageMembers = state.identity?.organization?.role === "org_admin" || state.identity?.user?.platform_role === "platform_admin";
    const [organizations, aiConnections, organizationMembers, integrations, rubrics, batches] = await Promise.all([
      api("/organizations"),
      api("/ai-connections"),
      canManageMembers ? api(`/organizations/${state.currentOrganizationId}/members`) : Promise.resolve([]),
      api("/system/integrations"),
      api("/rubrics"),
      api("/batches"),
    ]);
    state.organizations = organizations;
    state.aiConnections = aiConnections;
    state.organizationMembers = organizationMembers;
    state.integrations = integrations;
    state.rubrics = rubrics;
    state.batches = batches;
    setSidebarUser(state.identity.user, { organizationName: state.organizations.find((item) => item.id === state.currentOrganizationId)?.name || "" });
  } else {
    state.aiConnections = [];
    state.organizationMembers = [];
    const [integrations, rubrics, batches] = await Promise.all([
      api("/system/integrations"),
      api("/rubrics"),
      api("/batches"),
    ]);
    state.integrations = integrations;
    state.rubrics = rubrics;
    state.batches = batches;
  }
  if (!state.rubrics.some((rubric) => rubric.id === state.selectedLifecycleRubricId)) {
    state.selectedLifecycleRubricId = state.rubrics[0]?.id || "";
  }
  if (!state.rubrics.some((rubric) => rubric.id === state.anchorRubricId)) {
    state.anchorRubricId = state.rubrics[0]?.id || "";
  }
  if (!state.selectedBatchId && state.batches[0]) state.selectedBatchId = state.batches[0].id;
  const selectedBatchId = state.selectedBatchId;
  const [rubricLifecycle, papers, runs, ranking, drift, batchScoreJob, anchors, exportLogs] = await Promise.all([
    state.selectedLifecycleRubricId
      ? api(`/rubrics/${state.selectedLifecycleRubricId}/execution-draft`).catch((error) => ({ error: error.message }))
      : Promise.resolve(null),
    selectedBatchId ? api(`/papers?batch_id=${selectedBatchId}`) : Promise.resolve([]),
    selectedBatchId ? api(`/scoring-runs?batch_id=${selectedBatchId}`) : Promise.resolve([]),
    selectedBatchId ? api(`/batches/${selectedBatchId}/ranking`) : Promise.resolve(null),
    selectedBatchId ? api(`/batches/${selectedBatchId}/drift`) : Promise.resolve(null),
    selectedBatchId ? api(`/batches/${selectedBatchId}/score-jobs/latest`).catch((error) => {
      if (error.status === 404) return null;
      throw error;
    }) : Promise.resolve(null),
    state.anchorRubricId ? api(`/calibration/anchors?rubric_id=${state.anchorRubricId}`) : Promise.resolve([]),
    selectedBatchId ? api(`/export-logs?batch_id=${selectedBatchId}`) : Promise.resolve([]),
  ]);
  state.rubricLifecycle = rubricLifecycle;
  state.papers = papers;
  if (!state.papers.some((paper) => paper.id === state.selectedPaperId)) {
    state.selectedPaperId = state.papers[0]?.id || "";
  }
  state.runs = runs;
  if (!state.runs.some((run) => run.id === state.selectedRunId)) {
    state.selectedRunId = state.runs[0]?.id || "";
  }
  state.ranking = ranking;
  state.drift = drift;
  state.batchScoreJob = batchScoreJob;
  state.anchors = anchors;
  state.exportLogs = exportLogs;
  render();
}

async function refreshAnchors() {
  state.anchors = state.anchorRubricId ? await api(`/calibration/anchors?rubric_id=${state.anchorRubricId}`) : [];
}

async function refreshBatchScoreJob() {
  if (!state.selectedBatchId) {
    state.batchScoreJob = null;
    return;
  }
  try {
    state.batchScoreJob = await api(`/batches/${state.selectedBatchId}/score-jobs/latest`);
  } catch (error) {
    if (error.status !== 404) throw error;
    state.batchScoreJob = null;
  }
}

async function refreshRubricLifecycle() {
  if (!state.selectedLifecycleRubricId) {
    state.rubricLifecycle = null;
    return;
  }
  try {
    state.rubricLifecycle = await api(`/rubrics/${state.selectedLifecycleRubricId}/execution-draft`);
  } catch (error) {
    state.rubricLifecycle = { error: error.message };
  }
}

function switchPage(page) {
  if (!pageMeta[page]) return;
  state.page = page;
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.page === page);
  });
  document.querySelectorAll(".page").forEach((section) => {
    section.classList.toggle("active", section.id === `page-${page}`);
  });
  document.querySelector("#page-title").textContent = pageMeta[page][0];
  document.querySelector("#page-subtitle").textContent = pageMeta[page][1];
  render();
}

function render() {
  renderSharedSelects();
  renderDashboard();
  renderRubrics();
  renderRubricLifecycle();
  renderCalibration();
  renderBatches();
  renderReview();
  renderExports();
  renderSettings();
}

function renderSharedSelects() {
  const rubricSelect = document.querySelector('#batch-form select[name="rubric_id"]');
  rubricSelect.innerHTML = state.rubrics.length
    ? optionHtml(state.rubrics, "id", (item) => `${item.name} · ${item.version} · ${statusLabel(item.status)}`)
    : '<option value="">请先创建评分模板</option>';
  rubricSelect.disabled = !state.rubrics.length;

  const aiConnectionSelect = document.querySelector("#batch-ai-connection");
  if (aiConnectionSelect) {
    const activeConnections = state.aiConnections.filter((connection) => connection.status === "active");
    aiConnectionSelect.innerHTML = '<option value="">使用 Mock（不外发论文或私有 Key）</option>' + optionHtml(
      activeConnections,
      "id",
      (connection) => `${connection.name} · ${connection.provider_type} · ${connection.model_name} · ${connection.key_masked}`,
    );
  }

  for (const selector of ["#review-batch-select", "#export-batch-select"]) {
    document.querySelector(selector).innerHTML = optionHtml(state.batches, "id", (item) => item.name, state.selectedBatchId);
  }
}

function renderDashboard() {
  const totalPapers = state.papers.length;
  document.querySelector("#metric-batches").textContent = state.batches.length;
  document.querySelector("#metric-papers").textContent = totalPapers;
  document.querySelector("#metric-review").textContent = state.runs.filter((run) => run.need_manual_review).length;
  document.querySelector("#metric-failed").textContent = state.papers.filter((paper) => paper.status === "failed").length;
  renderActiveBatchSummary();
  renderIntegrationStatus();
  const recent = state.batches.slice(0, 5);
  document.querySelector("#dashboard-batches").innerHTML = recent.length
    ? `<table><thead><tr><th>任务</th><th>归属</th><th>状态</th><th>更新时间</th><th></th></tr></thead><tbody>${recent
        .map(
          (batch) => `<tr><td><strong>${escapeHtml(batch.name)}</strong></td><td>${escapeHtml([batch.department, batch.major].filter(Boolean).join(" · ") || "—")}</td><td><span class="badge ${statusTone(batch.status)}">${escapeHtml(statusLabel(batch.status))}</span></td><td>${escapeHtml((batch.created_at || "").slice(0, 16).replace("T", " "))}</td><td><button class="text-button" data-open-batch="${batch.id}">继续</button></td></tr>`,
        )
        .join("")}</tbody></table>`
    : '<div class="muted">还没有评分任务。创建任务后会在这里显示进度。</div>';
  renderBatchAnalytics();
}

function renderActiveBatchSummary() {
  const container = document.querySelector("#active-batch-summary");
  if (!container) return;
  const batch = state.batches.find((item) => item.id === state.selectedBatchId);
  if (!batch) {
    container.innerHTML = `<div class="continue-card"><div class="muted">还没有进行中的评分任务。</div><button class="primary full-width" data-navigate="batches" data-scroll-to="batch-form">创建第一个任务</button></div>`;
    return;
  }
  const job = state.batchScoreJob;
  let progress = 33;
  let next = "上传待评材料";
  if (state.papers.length) {
    progress = 66;
    next = "创建并运行批量评分";
  }
  if (job && ["completed", "completed_with_errors"].includes(job.status)) {
    progress = 100;
    next = "复核评分结果";
  } else if (job) {
    progress = 82;
    next = `${statusLabel(job.status)} · 查看运行状态`;
  }
  container.innerHTML = `<div class="continue-card">
    <div><h3>${escapeHtml(batch.name)}</h3><div class="muted">${escapeHtml([batch.department, batch.major].filter(Boolean).join(" · ") || "未设置归属")}</div></div>
    <div class="continue-meta"><span class="badge ${statusTone(batch.status)}">${escapeHtml(statusLabel(batch.status))}</span><span class="badge">${state.papers.length} 份材料</span><span class="badge">${state.runs.length} 条评分记录</span></div>
    <div><div class="item-title"><span class="muted">流程进度</span><strong>${progress}%</strong></div><div class="progress-track"><span style="width:${progress}%"></span></div></div>
    <button class="primary full-width" data-open-batch="${batch.id}">${escapeHtml(next)}</button>
  </div>`;
}

function renderBatchAnalytics() {
  const rankingEl = document.querySelector("#batch-ranking");
  const driftEl = document.querySelector("#batch-drift");
  if (!rankingEl || !driftEl) return;
  rankingEl.innerHTML = state.ranking
    ? renderTable(
        [
          { label: "名次", value: (row) => row.rank },
          { label: "论文", value: (row) => row.title || row.paper_id },
          { label: "学生", value: (row) => row.student_name || "" },
          { label: "总分", value: (row) => row.total },
          { label: "百分位", value: (row) => row.percentile ?? "" },
        ],
        state.ranking.ranking || [],
      )
    : '<div class="muted">请选择批次</div>';
  driftEl.innerHTML = state.drift
    ? renderTable(
        [
          { label: "评分项", value: (row) => row.criterion_name || row.criterion_code },
          { label: "AI 均分", value: (row) => row.mean_ai },
          { label: "人工均分", value: (row) => row.mean_final },
          { label: "偏移", value: (row) => `${row.bias}${row.flagged ? " ⚠" : ""}` },
          { label: "方向", value: (row) => row.direction },
          { label: "调整/样本", value: (row) => `${row.n_adjusted}/${row.n}` },
        ],
        state.drift.criteria || [],
      )
    : '<div class="muted">请选择批次</div>';
}

function renderIntegrationStatus() {
  const integrations = state.integrations;
  const container = document.querySelector("#integration-status");
  if (!integrations) {
    container.innerHTML = '<div class="muted">集成状态加载中</div>';
    return;
  }
  container.innerHTML = [
    integrationCard({
      title: "真实 LLM Adapter",
      active: integrations.llm.active,
      network: integrations.llm.network,
      primary: integrations.llm.active ? `${llmProviderLabel(integrations.llm)} 已启用` : "Mock 评分",
      detail: llmDetail(integrations.llm),
      note: integrations.llm.active ? llmActiveNote(integrations.llm) : llmFallbackNote(integrations.llm),
    }),
    integrationCard({
      title: "在线表格写入",
      active: integrations.sheets.active,
      primary: integrations.sheets.active ? "Google Sheets 已启用" : "Mock 写表预览",
      detail: integrations.sheets.adapter,
      note: integrations.sheets.active ? "写表会提交到配置的 Web App。" : sheetFallbackNote(integrations.sheets),
    }),
    integrationCard({
      title: "正式 Web 前端",
      active: integrations.frontend.static_web_ready,
      primary: integrations.frontend.static_web_ready ? "正式前端已启用" : "静态前端未就绪",
      detail: `入口 ${integrations.frontend.entrypoint}`,
      note: integrations.frontend.streamlit_backup ? "Streamlit 仅保留为备用操作台。" : "",
    }),
  ].join("");
}

function integrationCard({ title, active, primary, detail, note, network }) {
  const netBadge = network
    ? `<span class="badge ${network === "external" ? "warn" : "ok"}">${escapeHtml(networkLabel(network))}</span>`
    : "";
  return `<article class="integration-card">
    <div class="integration-card-title">
      <span>${escapeHtml(title)}</span>
      <span>${netBadge}<span class="badge ${active ? "ok" : "warn"}">${active ? "真实/正式" : "待配置"}</span></span>
    </div>
    <strong>${escapeHtml(primary)}</strong>
    <div class="muted">${escapeHtml(detail)}</div>
    <div>${escapeHtml(note)}</div>
  </article>`;
}

function networkLabel(network) {
  return { offline: "离线·不触网", local: "本地·连本地端口", external: "外呼·云厂商" }[network] || network || "";
}

function llmFallbackNote(llm) {
  if (llm.provider === "openai" && !llm.api_key_configured) return "已选择 OpenAI，但缺少 OPENAI_API_KEY。";
  if (isGoogleGemini(llm) && !llm.api_key_configured) {
    return "已选择 Google AI Studio/Gemini，但缺少 OPENAI_COMPATIBLE_API_KEY。";
  }
  if (["openai_compatible", "zhipu", "bigmodel", "qwen", "dashscope"].includes(llm.provider) && !llm.api_key_configured) {
    return "已选择国内兼容模型，但缺少 OPENAI_COMPATIBLE_API_KEY。";
  }
  if (llm.provider === "mock") return "设置 LLM_PROVIDER=local（本地私有模型，离线）或 openai_compatible（云，需 API Key）后启用真实 LLM。";
  if (llm.fallback_to_mock) return "真实 LLM 未配置完整，当前回退 Mock。";
  return "当前未启用真实 LLM。";
}

function llmActiveNote(llm) {
  if (llm.fallback_to_mock) return "优先调用真实 LLM；失败时自动降级 Mock 并标记人工复核。";
  return "当前评分会调用真实 LLM；失败时返回接口错误，便于排查配置。";
}

function llmDetail(llm) {
  const parts = [llm.adapter, llm.model];
  if (llm.thinking_type) parts.push(`thinking ${llm.thinking_type}`);
  if (llm.response_format_json) parts.push("JSON mode");
  return parts.join(" / ");
}

function llmProviderLabel(llm) {
  if (isGoogleGemini(llm)) return "Google AI Studio / Gemini";
  if (["local", "llama", "llamacpp", "llama_cpp", "vllm", "ollama"].includes(llm.provider)) return "本地私有模型";
  if (["openai_compatible", "zhipu", "bigmodel"].includes(llm.provider)) return "国内兼容模型";
  if (["qwen", "dashscope"].includes(llm.provider)) return "阿里云百炼";
  if (llm.provider === "openai") return "OpenAI";
  return llm.provider || "LLM";
}

function isGoogleGemini(llm) {
  const provider = String(llm.provider || "").toLowerCase();
  const compatible = String(llm.compatible_provider || "").toLowerCase();
  return ["google", "gemini", "google_ai_studio"].includes(provider) || ["google", "gemini", "google_ai_studio"].includes(compatible);
}

function sheetFallbackNote(sheets) {
  if (["google_sheets", "google_apps_script"].includes(sheets.provider) && !sheets.webapp_url_configured) {
    return "已选择 Google Sheets，但缺少 GOOGLE_SHEETS_WEBAPP_URL。";
  }
  if (sheets.provider === "mock") return "设置 SHEET_WRITER_PROVIDER=google_sheets 和 Web App URL 后启用真实写表。";
  if (sheets.fallback_to_mock) return "真实写表未配置完整，当前回退 Mock。";
  return "当前未启用真实在线写表。";
}

function renderRubrics() {
  const count = document.querySelector("#rubric-count-badge");
  const visibleRubrics = state.rubricVisibilityFilter === "all"
    ? state.rubrics
    : state.rubrics.filter((rubric) => rubric.visibility === state.rubricVisibilityFilter);
  if (count) count.textContent = `${visibleRubrics.length} 个`;
  document.querySelector("#rubric-list").innerHTML =
    visibleRubrics
      .map(
        (rubric) => `<article class="item-card template-card ${rubric.id === state.selectedLifecycleRubricId ? "selected" : ""}">
          <div class="item-title"><span>${escapeHtml(rubric.name)}</span><span class="badge ${statusTone(rubric.status)}">${escapeHtml(statusLabel(rubric.status))}</span></div>
          <div class="muted">${escapeHtml(rubric.version)} · ${rubric.total_score} 分 · ${rubric.criteria.length} 项 · ${escapeHtml({ private: "仅自己", organization: "当前组织", system: "系统模板" }[rubric.visibility] || rubric.visibility)}</div>
          <div class="template-description">${escapeHtml(rubric.description || "暂无说明")}</div>
          <div class="toolbar compact-toolbar">
            ${rubric.status === "draft" ? `<button class="secondary" data-edit-rubric="${rubric.id}">调整</button>` : ""}
            <button class="secondary" data-open-rubric-lifecycle="${rubric.id}">审核进度</button>
            ${rubric.status === "published" ? `<button class="secondary" data-clone-rubric="${rubric.id}">复制新版</button>` : ""}
          </div>
        </article>`,
      )
      .join("") || '<div class="muted">暂无评分模板。可从 Excel 导入或空白新建。</div>';
  renderRubricImportPreview();
  renderCriteriaBuilder("new");
  renderRubricEditForm();
}

function renderSettings() {
  const identity = document.querySelector("#settings-identity");
  const organizationSwitcher = document.querySelector("#organization-switcher");
  const roleHint = document.querySelector("#organization-role-hint");
  const members = document.querySelector("#organization-members");
  const inviteForm = document.querySelector("#organization-invite-form");
  const invitationResult = document.querySelector("#organization-invitation-result");
  const resetTokenResult = document.querySelector("#organization-reset-token-result");
  const connectionList = document.querySelector("#ai-connection-list");
  if (!identity || !organizationSwitcher || !roleHint || !members || !inviteForm || !invitationResult || !resetTokenResult || !connectionList) return;
  if (!state.authRequired || !state.identity?.user) {
    identity.textContent = "本地模式（未启用登录）";
    organizationSwitcher.innerHTML = '<option>启用认证后可管理组织</option>';
    organizationSwitcher.disabled = true;
    roleHint.textContent = "私有 AI 连接需要已登录的组织身份。";
    members.innerHTML = '<div class="muted">启用认证后可查看当前组织成员。</div>';
    inviteForm.classList.add("hidden");
    invitationResult.classList.add("hidden");
    resetTokenResult.classList.add("hidden");
    connectionList.innerHTML = '<div class="muted">启用认证后可配置私有 AI 连接。</div>';
    return;
  }
  identity.textContent = state.identity.user.display_name || state.identity.user.username;
  organizationSwitcher.disabled = false;
  organizationSwitcher.innerHTML = optionHtml(state.organizations, "id", (organization) => `${organization.name} · ${organization.role}`, state.currentOrganizationId);
  roleHint.textContent = `当前角色：${state.identity.organization?.role || "平台管理员"}`;
  const canManageMembers = state.identity.organization?.role === "org_admin" || state.identity.user.platform_role === "platform_admin";
  members.innerHTML = canManageMembers
    ? (state.organizationMembers.length
      ? `<table><thead><tr><th>成员</th><th>邮箱</th><th>角色</th><th></th></tr></thead><tbody>${state.organizationMembers.map((member) => `<tr><td>${escapeHtml(member.display_name || member.username)}</td><td>${escapeHtml(member.email || "—")}</td><td><select data-member-role="${escapeHtml(member.user_id)}"><option value="member" ${member.role === "member" ? "selected" : ""}>成员</option><option value="teacher" ${member.role === "teacher" ? "selected" : ""}>教师</option><option value="org_admin" ${member.role === "org_admin" ? "selected" : ""}>组织管理员</option></select></td><td><div class="action-row"><button class="secondary" data-member-update="${escapeHtml(member.user_id)}">保存角色</button><button class="secondary" data-member-reset="${escapeHtml(member.user_id)}" data-member-name="${escapeHtml(member.display_name || member.username)}">生成重置令牌</button></div></td></tr>`).join("")}</tbody></table>`
      : '<div class="muted">暂无成员。</div>')
    : '<div class="muted">仅当前组织管理员可以查看和邀请成员。</div>';
  inviteForm.classList.toggle("hidden", !canManageMembers);
  if (canManageMembers && state.latestInvitationLink) {
    invitationResult.classList.remove("hidden");
    invitationResult.innerHTML = `<strong>注册链接已创建（仅此一次显示）</strong><p class="muted">请通过受信任渠道安全转发给被邀请人。</p><label>注册链接<input readonly value="${escapeHtml(state.latestInvitationLink)}" aria-label="注册链接" /></label><button type="button" class="secondary" data-copy-invitation-link>复制链接</button>`;
  } else {
    invitationResult.classList.add("hidden");
    invitationResult.innerHTML = "";
  }
  if (canManageMembers && state.latestPasswordReset) {
    resetTokenResult.classList.remove("hidden");
    resetTokenResult.innerHTML = `<strong>${escapeHtml(state.latestPasswordReset.memberName)} 的重置链接已生成（仅此一次显示）</strong><p class="muted">请通过受信任渠道交给该用户；生成新的令牌会立即作废旧令牌。</p><label>重置链接<input readonly value="${escapeHtml(state.latestPasswordReset.url)}" aria-label="重置链接" /></label><button type="button" class="secondary" data-copy-reset-token>复制重置链接</button>`;
  } else {
    resetTokenResult.classList.add("hidden");
    resetTokenResult.innerHTML = "";
  }
  connectionList.innerHTML = state.aiConnections.length
    ? state.aiConnections.map((connection) => `<article class="item-card"><div class="item-title"><span>${escapeHtml(connection.name)}</span><span class="badge ${connection.status === "active" ? "ok" : "warn"}">${escapeHtml(connection.status)}</span></div><div class="muted">${escapeHtml(connection.provider_type)} · ${escapeHtml(connection.model_name)} · ${escapeHtml(connection.key_masked)} · v${connection.key_version}</div><div class="toolbar compact-toolbar"><button class="secondary" data-ai-test="${connection.id}">测试</button><button class="secondary" data-ai-rotate="${connection.id}">换 Key</button>${connection.status === "active" ? `<button class="secondary" data-ai-disable="${connection.id}">停用</button>` : ""}<button class="secondary" data-ai-delete="${connection.id}">删除</button></div></article>`).join("")
    : '<div class="muted">还没有私有 AI 连接。保存前可先测试配置。</div>';
}

function builderCriteria(scope) {
  return scope === "new" ? state.newCriteriaDraft : state.editCriteriaDraft;
}

function selectedOption(value, expected) {
  return value === expected ? "selected" : "";
}

function renderAIRuleDraft(criterion) {
  const draft = state.aiRuleDrafts[criterion.code];
  if (!draft) return "";
  const groups = draft.rule_groups || [];
  const rows = groups.flatMap((group) => (group.rules || []).map((rule) => `
    <tr><td>${escapeHtml(group.issue || group.group_code)}</td><td>${escapeHtml({ minor: "轻微", moderate: "中等", severe: "严重" }[rule.severity] || rule.severity)}</td><td>${escapeHtml(rule.trigger)}</td><td>${escapeHtml(rule.points)} 分</td></tr>`)).join("");
  return `<div class="ai-rule-draft" data-ai-draft-code="${escapeHtml(criterion.code)}">
    <div class="item-title"><span>AI 起草建议</span><span class="badge warn">待确认</span></div>
    <div class="muted">共 ${escapeHtml(groups.length)} 组规则；确认前不会进入正式评分规则。</div>
    <div class="table-wrap"><table><thead><tr><th>问题</th><th>程度</th><th>触发条件</th><th>扣分</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="toolbar"><button type="button" class="secondary" data-confirm-ai-rules="${escapeHtml(criterion.code)}">确认并应用此项</button><button type="button" class="secondary" data-draft-criterion="${escapeHtml(criterion.code)}">重新生成</button></div>
  </div>`;
}

function renderCriteriaBuilder(scope) {
  const criteria = builderCriteria(scope);
  const container = document.querySelector(`#${scope}-criteria-builder`);
  const total = document.querySelector(`#${scope}-criteria-total`);
  if (!container || !total) return;
  const totalScore = criteria.reduce((sum, item) => sum + Number(item.max_score || 0), 0);
  total.textContent = `共 ${criteria.length} 项 · ${totalScore} 分`;
  container.innerHTML = criteria.length
    ? criteria
        .map(
          (item, index) => `<article class="criterion-editor" data-criterion-code="${escapeHtml(item.code || "")}">
            <div class="criterion-editor-header"><span><span class="badge">${index + 1}</span>${escapeHtml(item.name || "未命名评分项")}</span><div class="toolbar">${scope === "edit" ? `<button type="button" class="secondary" data-draft-criterion="${escapeHtml(item.code || "")}">AI 起草</button>` : ""}<button type="button" data-remove-criterion="${scope}" data-index="${index}">删除</button></div></div>
            <div class="criterion-fields">
              <label>编码<input data-builder-scope="${scope}" data-index="${index}" data-criterion-field="code" value="${escapeHtml(item.code || "")}" /></label>
              <label>名称<input data-builder-scope="${scope}" data-index="${index}" data-criterion-field="name" value="${escapeHtml(item.name || "")}" /></label>
              <label>满分<input type="number" min="0.5" step="0.5" data-builder-scope="${scope}" data-index="${index}" data-criterion-field="max_score" value="${escapeHtml(item.max_score ?? 0)}" /></label>
              <label>判定方式<select data-builder-scope="${scope}" data-index="${index}" data-criterion-field="criterion_type"><option value="llm_judgment" ${selectedOption(item.criterion_type || "llm_judgment", "llm_judgment")}>智能判断</option><option value="deterministic" ${selectedOption(item.criterion_type, "deterministic")}>确定性检查</option><option value="hybrid" ${selectedOption(item.criterion_type, "hybrid")}>混合检查</option></select></label>
              <label>计分方式<select data-builder-scope="${scope}" data-index="${index}" data-criterion-field="scoring_mode"><option value="review_only" ${selectedOption(item.scoring_mode || ((item.deduction_rules || []).length ? "deductive" : "review_only"), "review_only")}>仅人工复核</option><option value="deductive" ${selectedOption(item.scoring_mode || ((item.deduction_rules || []).length ? "deductive" : "review_only"), "deductive")}>逐项扣分</option><option value="banded" ${selectedOption(item.scoring_mode, "banded")}>档位评分</option></select></label>
              <label class="wide">评分说明<textarea rows="2" data-builder-scope="${scope}" data-index="${index}" data-criterion-field="description">${escapeHtml(item.description || "")}</textarea></label>
              <label class="half-wide">证据位置提示<input data-builder-scope="${scope}" data-index="${index}" data-criterion-field="evidence_hints" value="${escapeHtml((item.evidence_hints || []).join("，"))}" placeholder="用逗号分隔" /></label>
              <label class="half-wide">基础扣分说明<textarea rows="2" data-builder-scope="${scope}" data-index="${index}" data-criterion-field="deduction_rules" placeholder="每行一条">${escapeHtml((item.deduction_rules || []).join("\n"))}</textarea></label>
            </div>
            ${scope === "edit" ? renderAIRuleDraft(item) : ""}
          </article>`,
        )
        .join("")
    : '<div class="muted">至少添加一个评分项。</div>';
  syncCriteriaTextarea(scope);
}

function syncCriteriaTextarea(scope) {
  const form = document.querySelector(scope === "new" ? "#rubric-form" : "#rubric-edit-form");
  if (!form?.elements.criteria) return;
  form.elements.criteria.value = JSON.stringify(builderCriteria(scope).map((item, index) => ({ ...item, display_order: index + 1 })), null, 2);
}

function renderRubricImportPreview() {
  const container = document.querySelector("#rubric-import-preview");
  if (!container) return;
  const imported = state.rubricImportPreview;
  if (!imported) {
    container.innerHTML = '<div class="muted">导入后将在此显示解析摘要、警告与待审核提示。</div>';
    return;
  }
  const warnings = imported.warnings || [];
  const summary = imported.template_summary || {};
  const rubric = imported.rubric || {};
  container.innerHTML = `<article class="item-card">
    <div class="item-title"><span>导入预检：${escapeHtml(rubric.name || "未命名标准")}</span><span class="badge ${warnings.length ? "warn" : "ok"}">${warnings.length ? `${warnings.length} 条警告` : "解析完成"}</span></div>
    <div class="muted">已生成 ${escapeHtml((rubric.criteria || []).length)} 个评分项；请继续完成规则与模板映射审核，预检完成不等于可发布。</div>
    ${warnings.length ? `<ul>${warnings.map((item) => `<li>${escapeHtml(lifecycleIssueText(item))}</li>`).join("")}</ul>` : '<div class="muted">没有解析警告。</div>'}
    <details><summary>模板解析摘要</summary><pre class="lifecycle-json">${escapeHtml(JSON.stringify(summary, null, 2))}</pre></details>
  </article>`;
}

function lifecycleButton(action, label, attributes = {}) {
  const data = Object.entries(attributes)
    .map(([key, value]) => ` data-${key}="${escapeHtml(value)}"`)
    .join("");
  return `<button class="secondary" data-lifecycle-action="${escapeHtml(action)}"${data}>${escapeHtml(label)}</button>`;
}

const LIFECYCLE_ISSUE_CODE_LABELS = {
  HYBRID_WEIGHT_NOT_EXACT: "混合评分权重无法精确表示",
  MISSING_CRITERION_DESCRIPTION: "缺少评分项说明",
  MISSING_EXECUTABLE_SCORING_MODE: "缺少可执行评分方式",
  TEMPLATE_FORMAT_PARSE_WARNING: "模板格式解析警告",
};

// 已保存的历史编译记录仍可能包含旧版英文消息，在展示层兼容翻译，
// 这样升级前导入的评分标准也无需重新导入即可显示中文提示。
const LEGACY_LIFECYCLE_ISSUE_MESSAGES = {
  "hybrid leaf weights are not exactly representable": "混合评分项的子项权重无法精确表示",
  "positive-score criterion requires a scoring description, band, deduction rule, or deterministic checker": "分值大于零的评分项必须配置评分说明、分档、扣分规则或确定性检查器",
  "criterion requires an explicit band, deduct or review-only mapping": "评分项必须明确配置分档评分、扣分评分或仅人工复核",
  "deductive criterion has no valid structured deduction rule": "扣分制评分项没有有效的结构化扣分规则",
  "manual criterion requires explicit executable mapping": "手工评分项必须明确配置可执行的评分方式",
};

function lifecycleIssueText(item) {
  if (typeof item === "string") return LEGACY_LIFECYCLE_ISSUE_MESSAGES[item] || item;
  if (!item || typeof item !== "object") return String(item ?? "");
  const code = item.code ? `[${LIFECYCLE_ISSUE_CODE_LABELS[item.code] || item.code}]` : "";
  const criterion = item.criterion_code ? `（评分项 ${item.criterion_code}）` : "";
  const rawMessage = item.message || item.detail || JSON.stringify(item);
  const message = LEGACY_LIFECYCLE_ISSUE_MESSAGES[rawMessage] || rawMessage;
  return `${code}${criterion}${code || criterion ? " " : ""}${message}`;
}

function renderLifecycleIssues(active, ambiguity) {
  const container = document.querySelector("#rubric-lifecycle-blockers");
  if (!container) return;
  const blockers = active?.blockers || [];
  const warnings = active?.warnings || [];
  const rows = [];
  if (ambiguity) rows.push({ tone: "error", label: "歧义", severityLabel: "错误", value: ambiguity });
  blockers.forEach((item) => rows.push({ tone: "error", label: "阻断", severityLabel: "错误", value: lifecycleIssueText(item), issue: item }));
  warnings.forEach((item) => rows.push({ tone: "warn", label: "警告", severityLabel: "警告", value: lifecycleIssueText(item) }));
  container.innerHTML = rows.length
    ? rows.map((item) => `<article class="item-card lifecycle-issue ${item.tone}"><div class="item-title"><span>${escapeHtml(item.label)}</span><span class="badge ${item.tone}">${escapeHtml(item.severityLabel)}</span></div><div>${escapeHtml(item.value)}</div>${item.issue?.criterion_code ? `<div class="toolbar"><button type="button" class="secondary" data-fix-blocker="${escapeHtml(item.issue.criterion_code)}" data-fix-field="${escapeHtml(item.issue.field_path || "")}">去修改 ${escapeHtml(item.issue.criterion_code)}</button></div>` : ""}</article>`).join("")
    : '<div class="muted">当前执行草稿没有警告或阻断项；仍须完成全部人工签核。</div>';
}

function renderLifecycleRules(rules = [], canReview = true) {
  const container = document.querySelector("#rubric-lifecycle-rules");
  if (!container) return;
  if (!rules.length) {
    container.innerHTML = '<div class="muted">当前执行草稿没有可审核规则。</div>';
    return;
  }
  const rows = rules.map((rule) => {
    let actions = '<span class="muted">无可用动作</span>';
    if (!canReview) {
      actions = '<span class="muted">请先处理执行草稿阻断项</span>';
    } else if (rule.status === "draft") {
      actions = lifecycleButton("rule-submit", "提交审核", { "rule-code": rule.rule_code });
    } else if (rule.status === "review") {
      actions = `${lifecycleButton("rule-approve", "批准", { "rule-code": rule.rule_code })}${lifecycleButton("rule-reject", "驳回", { "rule-code": rule.rule_code })}`;
    } else if (rule.status === "rejected") {
      actions = lifecycleButton("rule-reopen", "重新打开", { "rule-code": rule.rule_code });
    }
    return `<tr><td>${escapeHtml(rule.rule_code)}</td><td>${escapeHtml(rule.name)}</td><td>${escapeHtml(rule.judge_type)} / ${escapeHtml(rule.direction)}</td><td><span class="badge">${escapeHtml(rule.status)}</span></td><td><div class="toolbar">${actions}</div></td></tr>`;
  }).join("");
  const draftCount = rules.filter((rule) => rule.status === "draft").length;
  const reviewCount = rules.filter((rule) => rule.status === "review").length;
  const bulkActions = canReview
    ? `<div class="toolbar">${draftCount ? lifecycleButton("rules-submit-all", `批量提交 ${draftCount} 条评分规则`) : ""}${reviewCount ? lifecycleButton("rules-approve-all", `批量批准 ${reviewCount} 条评分规则`) : ""}</div>`
    : "";
  container.innerHTML = `${bulkActions}<table><thead><tr><th>规则</th><th>名称</th><th>判定/方向</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderLifecycleTemplateLinks(links = []) {
  const container = document.querySelector("#rubric-lifecycle-template-links");
  if (!container) return;
  if (!links.length) {
    container.innerHTML = '<div class="muted">该活动版本没有待确认模板映射。</div>';
    return;
  }
  const rows = links.map((link) => {
    const actions = link.review_status === "pending"
      ? `${lifecycleButton("link-confirm", "确认", { "link-id": link.id })}${lifecycleButton("link-reject", "驳回", { "link-id": link.id })}`
      : '<span class="muted">已完成</span>';
    return `<tr><td>${escapeHtml(link.id)}</td><td>${escapeHtml(link.rule_code)}</td><td><span class="badge">${escapeHtml(link.review_status)}</span></td><td><div class="toolbar">${actions}</div></td></tr>`;
  }).join("");
  container.innerHTML = `<table><thead><tr><th>映射 ID</th><th>规则</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderRubricLifecycle() {
  const select = document.querySelector("#rubric-lifecycle-select");
  const summary = document.querySelector("#rubric-lifecycle-summary");
  if (!select || !summary) return;
  select.innerHTML = optionHtml(state.rubrics, "id", (item) => `${item.name} / ${item.version} / ${item.status}`, state.selectedLifecycleRubricId);
  select.disabled = !state.rubrics.length;

  const lifecycle = state.rubricLifecycle;
  if (!lifecycle) {
    summary.innerHTML = '<div class="muted">请选择评分标准。</div>';
    renderLifecycleIssues(null, null);
    renderLifecycleRules([]);
    renderLifecycleTemplateLinks([]);
    return;
  }
  if (lifecycle.error) {
    summary.innerHTML = `<article class="item-card lifecycle-issue error"><strong>无法读取严格生命周期</strong><div>${escapeHtml(lifecycle.error)}</div></article>`;
    renderLifecycleIssues(null, lifecycle.error);
    renderLifecycleRules([]);
    renderLifecycleTemplateLinks([]);
    return;
  }

  const active = lifecycle.active_compilation;
  const version = active?.version || {};
  const selectedRubric = state.rubrics.find((item) => item.id === lifecycle.rubric_id);
  const actions = [];
  if (lifecycle.rubric_status === "draft") {
    const blockerCount = (active?.blockers || []).length;
    if (active?.status === "validated" && blockerCount === 0) {
      actions.push(lifecycleButton("rubric-submit", "提交模板审核"));
    } else {
      actions.push(`<button class="secondary" disabled>请先处理 ${escapeHtml(blockerCount)} 个阻断项</button>`);
    }
  }
  if (lifecycle.rubric_status === "review") {
    actions.push(lifecycleButton("rubric-return", "退回草稿"));
    if (active?.status === "validated") actions.push(lifecycleButton("rubric-publish", "发布此版本"));
  }
  summary.innerHTML = `<article class="item-card">
    <div class="item-title"><span>${escapeHtml(selectedRubric?.name || "评分模板")}</span><span class="badge ${statusTone(lifecycle.rubric_status)}">${escapeHtml(statusLabel(lifecycle.rubric_status))}</span></div>
    ${active ? `<div>执行草稿 <code>${escapeHtml(shortId(active.id))}</code> · <span class="badge ${active.status === "validated" ? "ok" : "warn"}">${escapeHtml(active.status === "validated" ? "校验通过" : "存在阻断")}</span></div>
      <div class="muted">校验版本 ${escapeHtml(version.version || "-")}</div>` : `<div class="muted">${escapeHtml(lifecycle.ambiguity || "当前没有可审核的执行草稿，请返回上一步保存模板。")}</div>`}
    <div class="toolbar">${actions.join("") || '<span class="muted">完成当前待办后，下一步操作会在这里出现。</span>'}</div>
    <details><summary>查看执行草稿历史</summary><pre class="lifecycle-json">${escapeHtml(JSON.stringify(lifecycle.compilations || [], null, 2))}</pre></details>
  </article>`;
  renderLifecycleIssues(active, lifecycle.ambiguity);
  renderLifecycleRules(active?.rules || [], active?.status === "validated");
  renderLifecycleTemplateLinks(active?.template_links || []);
}

function renderRubricEditForm() {
  const form = document.querySelector("#rubric-edit-form");
  const select = document.querySelector("#rubric-edit-select");
  const draftRubrics = state.rubrics.filter((rubric) => rubric.status === "draft");
  if (!draftRubrics.some((rubric) => rubric.id === state.selectedRubricId)) {
    state.selectedRubricId = draftRubrics[0]?.id || "";
  }
  const rubric = draftRubrics.find((item) => item.id === state.selectedRubricId);
  select.innerHTML = draftRubrics.length
    ? optionHtml(draftRubrics, "id", (item) => `${item.name} · ${item.version}`, state.selectedRubricId)
    : '<option value="">暂无可编辑草稿</option>';
  select.disabled = !draftRubrics.length;

  form.elements.name.value = rubric?.name || "";
  form.elements.version.value = rubric?.version || "";
  form.elements.description.value = rubric?.description || "";
  if (rubric?.id !== state.editCriteriaRubricId) {
    state.editCriteriaRubricId = rubric?.id || "";
    state.editCriteriaDraft = rubric ? criteriaPayloadRows(rubric.criteria || []) : [];
    state.aiRuleDrafts = {};
    state.rubricEditStatus = "clean";
    state.rubricEditError = null;
  }
  renderCriteriaBuilder("edit");
  for (const field of ["name", "version", "description", "criteria"]) {
    form.elements[field].disabled = !rubric;
  }
  form.querySelector('button[type="submit"]').disabled = !rubric;
  document.querySelector('[data-add-criterion="edit"]').disabled = !rubric;
  renderRubricEditFeedback();
}

function renderBatches() {
  const count = document.querySelector("#batch-count-badge");
  if (count) count.textContent = `${state.batches.length} 个`;
  document.querySelector("#batch-list").innerHTML =
    state.batches
      .map(
        (batch) => `<article class="item-card ${batch.id === state.selectedBatchId ? "selected" : ""}" data-select-batch="${batch.id}">
          <div class="item-title"><span>${escapeHtml(batch.name)}</span><span class="badge ${statusTone(batch.status)}">${escapeHtml(statusLabel(batch.status))}</span></div>
          <div class="muted">${escapeHtml([batch.department, batch.major].filter(Boolean).join(" · ") || "未设置归属")}</div>
        </article>`,
      )
      .join("") || '<div class="muted">暂无评分任务</div>';

  document.querySelector("#paper-list").innerHTML = state.papers.length
    ? `<table><thead><tr><th>材料</th><th>作者 / 编号</th><th>状态</th><th>解析质量</th><th>操作</th></tr></thead><tbody>${state.papers
        .map(
          (paper) => `<tr><td><strong>${escapeHtml(paper.title || paper.file_name)}</strong></td><td>${escapeHtml(paper.student_name || paper.student_id || "未识别")}</td><td><span class="badge ${statusTone(paper.status)}">${escapeHtml(statusLabel(paper.status))}</span>${paper.error_message ? `<div class="table-error">${escapeHtml(paper.error_message)}</div>` : ""}</td><td>${escapeHtml(paper.parse_quality ?? "—")}</td><td>${paper.status === "failed" ? `<button class="text-button" data-retry-parse="${paper.id}">重新解析</button>` : "—"}</td></tr>`,
        )
        .join("")}</tbody></table>`
    : '<div class="muted">选择左侧任务后，在此上传第一份待评材料。</div>';
  renderPaperUploadQueue();
  renderBatchWorkflowSteps();
  renderPaperEditForm();
  renderBatchScoreJob();
}

function renderBatchWorkflowSteps() {
  const container = document.querySelector("#batch-workflow-steps");
  if (!container) return;
  const hasBatch = Boolean(state.selectedBatchId);
  const hasPapers = state.papers.length > 0;
  const hasJob = Boolean(state.batchScoreJob);
  const steps = [
    { number: 1, label: "创建任务", state: hasBatch ? "complete" : "active" },
    { number: 2, label: "上传材料", state: hasPapers ? "complete" : hasBatch ? "active" : "" },
    { number: 3, label: "运行评分", state: hasJob ? (state.batchScoreJob.status === "completed" ? "complete" : "active") : hasPapers ? "active" : "" },
  ];
  container.innerHTML = steps.map((step) => `<span class="step ${step.state}"><b>${step.state === "complete" ? "✓" : step.number}</b>${step.label}</span>`).join("");
}

function renderBatchScoreJob() {
  const job = state.batchScoreJob;
  const status = document.querySelector("#batch-score-job-status");
  const errors = document.querySelector("#batch-score-job-errors");
  const signals = document.querySelector("#batch-score-job-signals");
  if (!status || !errors || !signals) return;
  const selected = Boolean(state.selectedBatchId);
  const hasUnparsedPapers = state.papers.some((paper) => paper.status !== "parsed");
  document.querySelector("#create-batch-score-job-btn").disabled = !selected || hasUnparsedPapers || Boolean(job && ["queued", "running", "cancel_requested"].includes(job.status));
  document.querySelector("#run-batch-score-job-btn").disabled = !job || job.status !== "queued";
  document.querySelector("#cancel-batch-score-job-btn").disabled = !job || !["queued", "running", "cancel_requested"].includes(job.status);
  document.querySelector("#retry-batch-score-job-btn").disabled = !job || !["canceled", "completed_with_errors", "failed"].includes(job.status);
  if (!job) {
    const pendingPapers = state.papers.filter((paper) => paper.status !== "parsed");
    status.innerHTML = state.papers.length
      ? pendingPapers.length
        ? `<div class="inline-error">请先处理 ${pendingPapers.length} 份尚未完成解析的材料，再运行批量评分。</div>`
        : '<div class="muted">材料已经就绪。填写下方经批准的观察策略，创建可恢复的批量评分任务。</div>'
      : '<div class="muted">上传至少一份待评材料后，才能进入批量评分。</div>';
    errors.innerHTML = '<div class="muted">暂无错误</div>';
    signals.innerHTML = '<div class="muted">任务运行后显示门禁信号</div>';
    return;
  }
  status.innerHTML = `<div class="metric-grid batch-job-metrics">
      <div class="metric"><span>运行状态</span><strong>${escapeHtml(statusLabel(job.status))}</strong><small>第 ${job.generation} 代任务</small></div>
      <div class="metric"><span>已完成</span><strong>${job.succeeded_count + job.skipped_count}/${job.total_items}</strong><small>成功与跳过</small></div>
      <div class="metric"><span>异常</span><strong>${job.failed_count + job.canceled_count}</strong><small>${job.failed_count} 失败 · ${job.canceled_count} 取消</small></div>
      <div class="metric"><span>运行配置</span><strong>${job.max_workers} 并发</strong><small>策略 ${escapeHtml((job.observation_policy_hash || "").slice(0, 10))}</small></div>
    </div>`;
  const failed = (job.items || []).filter((item) => item.error_code || item.error_message);
  errors.innerHTML = renderTable(
    [
      { label: "论文", value: (row) => row.paper_id },
      { label: "状态/次数", value: (row) => `${row.status}/${row.attempt_count}` },
      { label: "错误码", value: (row) => row.error_code || "" },
      { label: "错误", value: (row) => row.error_message || "" },
    ],
    failed,
  );
  const gate = job.metrics_snapshot?.gate;
  const rows = gate
    ? Object.entries(gate.signals || {}).map(([name, value]) => ({
        name,
        status: value.status,
        actual: value.actual ?? "",
        threshold: value.threshold ?? "",
      }))
    : [];
  const authorization = gate?.production_default_switch_authorized === true;
  signals.innerHTML = `${renderTable(
    [
      { label: "信号", value: (row) => row.name },
      { label: "判定", value: (row) => row.status },
      { label: "实际值", value: (row) => row.actual },
      { label: "阈值", value: (row) => row.threshold },
    ],
    rows,
  )}<div class="gate-authorization ${authorization ? "authorized" : "blocked"}">
    production_default_switch_authorized = ${authorization ? "true" : "false"}
  </div>`;
}

function renderPaperEditForm() {
  const form = document.querySelector("#paper-edit-form");
  const select = document.querySelector("#paper-edit-select");
  const paper = state.papers.find((item) => item.id === state.selectedPaperId);
  select.innerHTML = optionHtml(state.papers, "id", paperLabel, state.selectedPaperId);
  select.disabled = !state.papers.length;

  for (const field of ["title", "student_id", "student_name", "department", "major", "advisor"]) {
    form.elements[field].value = paper?.[field] || "";
    form.elements[field].disabled = !paper;
  }
  form.querySelector('button[type="submit"]').disabled = !paper;
}

function renderReview() {
  document.querySelector("#review-paper-select").innerHTML = optionHtml(state.papers, "id", paperLabel, state.selectedPaperId);
  document.querySelector("#run-select").innerHTML = optionHtml(state.runs, "id", runLabel, state.selectedRunId);
  renderLlmRuntimeStatus();
  const run = state.runs.find((item) => item.id === state.selectedRunId);
  document.querySelector("#run-summary").innerHTML = run
    ? `<div class="score-grid">
        <div class="metric"><span>AI 总分</span><strong>${run.ai_total_score ?? ""}</strong></div>
        <div class="metric"><span>最终总分</span><strong>${run.final_total_score ?? ""}</strong></div>
        <div class="metric"><span>等级</span><strong>${escapeHtml(run.grade || "")}</strong></div>
        <div class="metric"><span>需复核</span><strong>${run.need_manual_review ? "是" : "否"}</strong></div>
        <div class="metric"><span>Token</span><strong>${run.total_tokens ?? 0}</strong></div>
      </div>
      <div class="review-box">
        <label>整体复核意见<textarea id="review-reason" placeholder="说明确认结论或需要调整的原因"></textarea></label>
        <button id="submit-review-btn">确认并提交复核</button>
      </div>`
    : '<div class="muted">选择一条评分记录后，在这里查看总分、等级和复核状态。</div>';
  renderScoreItems();
  renderRunFindings();
}

function renderRunFindings() {
  const container = document.querySelector("#run-findings");
  if (!container) return;
  const run = state.runs.find((item) => item.id === state.selectedRunId);
  if (!run) {
    container.innerHTML = '<div class="muted">请选择评分任务</div>';
    return;
  }
  const coherence = run.coherence_findings || [];
  const format = run.format_findings || [];
  container.innerHTML = `<h3 class="section-title">篇章一致性（${coherence.length}）</h3>${findingsTableHtml(coherence)}<h3 class="section-title">格式问题（${format.length}）</h3>${findingsTableHtml(format)}`;
}

function findingsTableHtml(findings) {
  return renderTable(
    [
      { label: "级别", value: (row) => row.severity || "" },
      { label: "类型", value: (row) => row.kind || row.field || "" },
      { label: "说明", value: (row) => row.message || "" },
      { label: "计入扣分", value: (row) => (row.deducted_by ? `−${row.deducted_points ?? ""}（${row.deducted_by}）` : "") },
    ],
    findings || [],
  );
}

function deductionItemsHtml(items) {
  const scored = (items || []).filter((item) => item.points != null);
  if (!scored.length) return "";
  const text = scored
    .map((item) => `−${item.points} ${item.reason || ""}${item.rule_ref ? `(${item.rule_ref})` : ""}`)
    .join("；");
  return `<div class="muted">结构化扣分：${escapeHtml(text)}</div>`;
}

function renderCalibration() {
  const select = document.querySelector("#anchor-rubric-select");
  if (!select) return;
  select.innerHTML = optionHtml(state.rubrics, "id", (item) => `${item.name} / ${item.version}`, state.anchorRubricId);
  document.querySelector("#anchor-list").innerHTML = renderTable(
    [
      { label: "评分项", value: (row) => row.criterion_code },
      { label: "档位", value: (row) => row.label || "" },
      { label: "分/满分", value: (row) => `${row.score}/${row.max_score}` },
      { label: "范文摘录", value: (row) => (row.excerpt || "").slice(0, 40) },
    ],
    state.anchors || [],
  );
}

function renderLlmRuntimeStatus() {
  const container = document.querySelector("#llm-runtime-status");
  if (!container) return;
  const status = state.llmStatus || { phase: "idle" };
  const llm = state.integrations?.llm || {};
  const paper = state.papers.find((item) => item.id === state.selectedPaperId);
  const isProcessing = status.phase === "processing";
  const button = document.querySelector("#score-paper-btn");
  if (button) {
    button.disabled = isProcessing || !paper;
    button.textContent = isProcessing ? "评分中..." : "评分选中材料";
  }

  if (status.phase === "processing") {
    const elapsedSeconds = Math.max(0, Math.floor((Date.now() - status.startedAt) / 1000));
    container.innerHTML = llmStatusHtml({
      tone: "active",
      badge: "LLM 处理中",
      title: "正在评分选中材料",
      detail: `${paper ? paperLabel(paper) : "未选择论文"} · 已运行 ${elapsedSeconds}s`,
      note: llmProcessingNote(llm),
    });
    return;
  }

  if (status.phase === "done") {
    const fallbackText = status.fallbackCount
      ? `检测到 ${status.fallbackCount}/${status.itemCount} 个评分项降级为 Mock，已标记人工复核。`
      : "未检测到 Mock 降级痕迹。";
    container.innerHTML = llmStatusHtml({
      tone: status.fallbackCount ? "warn" : "ok",
      badge: status.fallbackCount ? "已降级" : "完成",
      title: "LLM 评分请求已完成",
      detail: `总分 ${status.run?.final_total_score ?? ""} · 等级 ${status.run?.grade || ""} · 需复核 ${status.run?.need_manual_review ? "是" : "否"}`,
      note: fallbackText,
    });
    return;
  }

  if (status.phase === "error") {
    container.innerHTML = llmStatusHtml({
      tone: "error",
      badge: "调用失败",
      title: "LLM 评分请求失败",
      detail: status.message || "未知错误",
      note: "可运行 uv run python -m backend.app.scripts.diagnose_llm 检查真实模型网络、TLS、Key 和响应结构。",
    });
    return;
  }

  container.innerHTML = llmStatusHtml({
    tone: llm.active ? "ok" : "idle",
    badge: llm.active ? "真实 LLM 就绪" : "Mock 模式",
    title: llm.active ? `${llmProviderLabel(llm)} 可用` : "当前未启用真实 LLM",
    detail: llmDetail(llm),
    note: llm.active ? llmProcessingNote(llm) : llmFallbackNote(llm),
  });
}

function llmStatusHtml({ tone, badge, title, detail, note }) {
  const spinner = tone === "active" ? '<span class="llm-spinner"></span>' : "";
  return `<div class="llm-status-header">
      <span class="llm-status-title">${spinner}${escapeHtml(title)}</span>
      <span class="badge ${toneClass(tone)}">${escapeHtml(badge)}</span>
    </div>
    <div class="muted">${escapeHtml(detail || "")}</div>
    <div>${escapeHtml(note || "")}</div>`;
}

function toneClass(tone) {
  if (tone === "ok") return "ok";
  if (tone === "warn" || tone === "active") return "warn";
  if (tone === "error") return "error";
  return "";
}

function llmProcessingNote(llm) {
  if (!llm.active) return "当前会使用 Mock 评分器，不会请求外部模型。";
  const fallback = llm.fallback_to_mock ? "真实调用失败时会自动降级 Mock，并强制进入人工复核。" : "真实调用失败时会返回错误。";
  const jsonMode = llm.response_format_json ? "JSON mode 已开启。" : "JSON mode 未开启。";
  const thinking = llm.thinking_type ? `thinking ${llm.thinking_type}。` : "";
  return `${llmDetail(llm)}。按证据块逐块评分并由后端汇总；分值、等级、舍入与复核边界来自本次运行的冻结 ScoringPolicy，正式规则分值来自已发布 RubricVersion。${thinking}${jsonMode}${fallback}`;
}

async function renderScoreItems() {
  const container = document.querySelector("#score-items");
  if (!state.selectedRunId) {
    container.innerHTML = '<div class="muted">请选择评分任务</div>';
    return;
  }
  try {
    const run = state.runs.find((item) => item.id === state.selectedRunId);
    const [items, chunks] = await Promise.all([
      api(`/scoring-runs/${state.selectedRunId}/items`),
      run ? api(`/papers/${run.paper_id}/chunks`) : Promise.resolve([]),
    ]);
    state.chunksById = Object.fromEntries(chunks.map((chunk) => [chunk.id, chunk]));
    container.innerHTML = items.map(scoreItemHtml).join("") || '<div class="muted">暂无评分明细</div>';
  } catch (error) {
    container.innerHTML = `<div class="muted">${escapeHtml(error.message)}</div>`;
  }
}

function scoreItemHtml(item) {
  const evidence = (item.evidence || [])
    .map((ev) => {
      const chunk = state.chunksById[ev.chunk_id];
      return `<div class="evidence">${escapeHtml(ev.location || "")}<br>${escapeHtml(ev.quote || "")}</div>${
        chunk ? `<pre class="context">${escapeHtml(formatChunk(chunk))}</pre>` : ""
      }`;
    })
    .join("");
  return `<article class="score-card">
    <div class="item-title">
      <span>${escapeHtml(item.criterion_name || item.criterion_id)}</span>
      <span class="badge ${item.need_manual_review ? "warn" : "ok"}">${item.final_score}/${item.max_score}</span>
    </div>
    <div>${escapeHtml(item.reason)}</div>
    <div class="muted">扣分：${escapeHtml((item.deductions || []).join("；"))}</div>
    ${deductionItemsHtml(item.deduction_items)}
    <div class="muted">建议：${escapeHtml(item.suggestion || "")}</div>
    ${evidence}
    <div class="review-box">
      <label>最终得分<input type="number" min="0" step="0.5" value="${item.final_score ?? item.ai_score}" data-score-input="${item.id}"></label>
      <button class="secondary" data-save-score="${item.id}">保存修改</button>
    </div>
  </article>`;
}

function renderExports() {
  document.querySelector("#export-run-select").innerHTML = optionHtml(state.runs, "id", runLabel, state.selectedRunId);
  document.querySelector("#export-logs").innerHTML = renderTable(
    [
      { label: "时间", value: (row) => (row.created_at || "").slice(0, 19) },
      { label: "评分任务", value: (row) => shortId(row.scoring_run_id) },
      { label: "目标类型", value: (row) => row.target_type },
      { label: "目标", value: (row) => row.target_id || "" },
      { label: "状态", value: (row) => row.status },
      { label: "结果", value: exportLogMessage },
    ],
    state.exportLogs,
  );
}

function paperLabel(paper) {
  return `${paper.student_name || paper.student_id || "未识别"} · ${paper.title || paper.file_name} · ${statusLabel(paper.status)}`;
}

function runLabel(run) {
  return `${(run.created_at || "").slice(0, 16).replace("T", " ")} · ${statusLabel(run.status)} · ${run.final_total_score ?? "—"} 分 · ${run.grade || "未定级"}`;
}

function formatChunk(chunk) {
  const title = chunk.section_title || "未知章节";
  const page = chunk.page_start ? `，第${chunk.page_start}${chunk.page_end && chunk.page_end !== chunk.page_start ? `-${chunk.page_end}` : ""}页` : "";
  return `${title}${page}\n\n${chunk.text || ""}`;
}

function shortId(value) {
  return value ? String(value).slice(0, 8) : "";
}

function optionalText(value) {
  const trimmed = String(value ?? "").trim();
  return trimmed || null;
}

function aiConnectionPayload(form) {
  return {
    name: String(form.get("name") || "").trim(),
    provider_type: String(form.get("provider_type") || "").trim(),
    base_url: String(form.get("base_url") || "").trim(),
    model_name: String(form.get("model_name") || "").trim(),
    provider_options: {},
    api_key: String(form.get("api_key") || ""),
  };
}

function criteriaPayloadRows(criteria) {
  return criteria
    .slice()
    .sort((left, right) => (left.display_order ?? 0) - (right.display_order ?? 0))
    .map((item, index) => ({
      code: item.code,
      name: item.name,
      max_score: item.max_score,
      weight: item.weight,
      description: item.description,
      evidence_hints: item.evidence_hints || [],
      deduction_rules: item.deduction_rules || [],
      display_order: item.display_order ?? index,
      criterion_type: item.criterion_type || "llm_judgment",
      scoring_mode: item.scoring_mode || ((item.deduction_rules || []).length ? "deductive" : "review_only"),
      applies_to: item.applies_to || "global",
      rubric_levels: item.rubric_levels || [],
      sub_checks: item.sub_checks || [],
      dimension: item.dimension ?? null,
      deduction_rules_structured: item.deduction_rules_structured || [],
    }));
}

function exportLogMessage(log) {
  if (log.error_message) return log.error_message;
  const response = log.response || {};
  if (response.preview) return `${response.preview.length} 行预览`;
  if (response.provider_response?.spreadsheet_url) return response.provider_response.spreadsheet_url;
  if (response.provider_response?.spreadsheet_id) return response.provider_response.spreadsheet_id;
  if (response.updatedRange) return response.updatedRange;
  if (response.spreadsheetUrl) return response.spreadsheetUrl;
  if (response.spreadsheet_url) return response.spreadsheet_url;
  return Object.keys(response).length ? JSON.stringify(response).slice(0, 140) : "";
}

async function refreshExportLogs() {
  state.exportLogs = state.selectedBatchId ? await api(`/export-logs?batch_id=${state.selectedBatchId}`) : [];
}

function requireLifecycleReason(label) {
  const reason = window.prompt(`${label}原因（写入审核记录）`, label);
  if (reason === null) return null;
  if (!reason.trim()) throw new Error("审核原因不能为空");
  return reason.trim();
}

async function handleRubricLifecycleAction(target) {
  const action = target.dataset.lifecycleAction;
  const rubricId = state.selectedLifecycleRubricId;
  const active = state.rubricLifecycle?.active_compilation;
  if (!action || !rubricId) throw new Error("请选择具有活动执行草稿的评分标准");

  if (action === "rubric-submit") {
    await api(`/rubrics/${rubricId}/submit-review`, { method: "POST" });
    showToast("Rubric 已提交审核；发布仍需全部规则和模板映射通过");
  } else if (action === "rubric-return") {
    await api(`/rubrics/${rubricId}/return-to-draft`, { method: "POST" });
    showToast("Rubric 已退回草稿，可继续处理规则或映射");
  } else if (action === "rubric-publish") {
    if (!active?.id) throw new Error("没有可发布的活动执行草稿");
    const reason = requireLifecycleReason("发布冻结版本");
    if (reason === null) return;
    await api(`/rubrics/${rubricId}/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ compilation_id: active.id, reason }),
    });
    showToast("评分模板已发布并冻结为不可修改的正式版本");
  } else if (action === "rules-submit-all" || action === "rules-approve-all") {
    const desiredStatus = action === "rules-submit-all" ? "draft" : "review";
    const suffix = action === "rules-submit-all" ? "/submit-review" : "/approve";
    const label = action === "rules-submit-all" ? "批量提交评分规则" : "批量批准评分规则";
    const rules = (active?.rules || []).filter((rule) => rule.status === desiredStatus);
    const reason = requireLifecycleReason(label);
    if (reason === null) return;
    for (const rule of rules) {
      await api(`/rubrics/${rubricId}/rules/${encodeURIComponent(rule.rule_code)}${suffix}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason }),
      });
    }
    showToast(`${label}已完成，共处理 ${rules.length} 条`);
  } else if (action.startsWith("rule-")) {
    const ruleCode = target.dataset.ruleCode;
    if (!ruleCode) throw new Error("缺少评分规则编号");
    const operation = {
      "rule-submit": ["/submit-review", "提交规则审核"],
      "rule-approve": ["/approve", "批准规则"],
      "rule-reject": ["/reject", "驳回规则"],
      "rule-reopen": ["/reopen", "重新打开规则"],
    }[action];
    if (!operation) throw new Error(`不支持的规则操作：${action}`);
    const reason = requireLifecycleReason(operation[1]);
    if (reason === null) return;
    await api(`/rubrics/${rubricId}/rules/${encodeURIComponent(ruleCode)}${operation[0]}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    });
    showToast(`${operation[1]}已完成`);
  } else if (action === "link-confirm" || action === "link-reject") {
    const linkId = target.dataset.linkId;
    if (!linkId) throw new Error("缺少模板映射 ID");
    const decision = action === "link-confirm" ? "confirmed" : "rejected";
    const reason = requireLifecycleReason(decision === "confirmed" ? "确认模板映射" : "驳回模板映射");
    if (reason === null) return;
    await api(`/rubrics/${rubricId}/template-links/${encodeURIComponent(linkId)}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, reason }),
    });
    showToast(`模板映射已${decision === "confirmed" ? "确认" : "驳回"}`);
  } else {
    throw new Error(`不支持的 Rubric 生命周期操作：${action}`);
  }
  await loadAll();
}

function revealAndScroll(id) {
  const element = document.getElementById(id);
  if (!element) return;
  if (element instanceof HTMLDetailsElement) element.open = true;
  const parentDetails = element.closest("details");
  if (parentDetails) parentDetails.open = true;
  window.setTimeout(() => element.scrollIntoView({ behavior: "smooth", block: "start" }), 0);
}

function nextCriterionCode(criteria) {
  const max = criteria.reduce((current, item) => {
    const match = String(item.code || "").match(/(\d+)$/);
    return Math.max(current, match ? Number(match[1]) : 0);
  }, 0);
  return `C${String(max + 1).padStart(2, "0")}`;
}

function addCriterion(scope) {
  const criteria = builderCriteria(scope);
  criteria.push({
    code: nextCriterionCode(criteria),
    name: "新评分项",
    max_score: 10,
    description: "",
    evidence_hints: [],
    deduction_rules: [],
    display_order: criteria.length + 1,
    criterion_type: "llm_judgment",
    scoring_mode: "review_only",
    applies_to: "global",
    rubric_levels: [],
    sub_checks: [],
    dimension: null,
    deduction_rules_structured: [],
  });
  if (scope === "edit") state.rubricEditStatus = "dirty";
  renderCriteriaBuilder(scope);
  renderRubricEditFeedback();
}

async function draftAIRules(criteria) {
  if (!state.selectedRubricId) throw new Error("请选择要调整的评分模板");
  state.rubricEditStatus = "generating";
  state.rubricEditError = null;
  renderRubricEditFeedback();
  const activeConnection = state.aiConnections.find((item) => item.status === "active");
  try {
    const result = await api(`/rubrics/${state.selectedRubricId}/draft-deduction-rules`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        criteria,
        ai_connection_id: activeConnection?.id || null,
      }),
    });
    for (const item of result.items || []) {
      if (item.draft) state.aiRuleDrafts[item.criterion_code] = item.draft;
    }
    state.rubricEditStatus = Object.keys(state.aiRuleDrafts).length ? "ai_draft_pending" : "dirty";
    renderCriteriaBuilder("edit");
    renderRubricEditFeedback();
    showToast("AI 扣分细则已生成，请确认后保存");
  } catch (error) {
    renderRubricOperationError(error);
  }
}

function applyConfirmedAIRules(criterionCode) {
  const draft = state.aiRuleDrafts[criterionCode];
  const criterion = state.editCriteriaDraft.find((item) => item.code === criterionCode);
  if (!draft || !criterion) return false;
  criterion.scoring_mode = "deductive";
  criterion.deduction_rules_structured = (draft.rule_groups || []).flatMap((group) =>
    (group.rules || []).map((rule, index) => ({
      match: rule.trigger,
      trigger: rule.trigger,
      points: rule.points,
      reason: rule.reason,
      severity: rule.severity,
      repeat_policy: rule.repeat_policy || "once",
      cap_points: group.cap_points,
      mutex_group: group.mutex_group,
      source: rule.source || "ai_inferred",
      source_refs: rule.source_refs || [],
      generation_fingerprint: draft.generation_metadata?.fingerprint,
      confirmed: true,
      display_order: index,
    })),
  );
  delete state.aiRuleDrafts[criterionCode];
  state.rubricEditStatus = "dirty";
  state.rubricEditError = null;
  syncCriteriaTextarea("edit");
  return true;
}

function confirmAllAIRules() {
  let confirmed = 0;
  for (const criterionCode of Object.keys(state.aiRuleDrafts)) {
    if (applyConfirmedAIRules(criterionCode)) confirmed += 1;
  }
  renderCriteriaBuilder("edit");
  renderRubricEditFeedback();
  if (confirmed) showToast(`已确认并应用 ${confirmed} 个评分项的 AI 建议`);
}

function updateCriterionField(element) {
  const scope = element.dataset.builderScope;
  const index = Number(element.dataset.index);
  const field = element.dataset.criterionField;
  const criterion = builderCriteria(scope)?.[index];
  if (!criterion || !field) return;
  if (field === "max_score") criterion[field] = Number(element.value);
  else if (field === "evidence_hints") criterion[field] = element.value.split(/[,，]/).map((value) => value.trim()).filter(Boolean);
  else if (field === "deduction_rules") criterion[field] = element.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  else criterion[field] = element.value;
  if (scope === "edit") {
    state.rubricEditStatus = "dirty";
    state.rubricEditError = null;
    delete state.aiRuleDrafts[criterion.code];
    renderRubricEditFeedback();
  }
  const total = document.querySelector(`#${scope}-criteria-total`);
  if (total) {
    const criteria = builderCriteria(scope);
    total.textContent = `共 ${criteria.length} 项 · ${criteria.reduce((sum, item) => sum + Number(item.max_score || 0), 0)} 分`;
  }
  syncCriteriaTextarea(scope);
}

async function handleAction(event) {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const batchCard = target.closest("[data-select-batch]");
  try {
    const navigateControl = target.closest("[data-navigate]");
    if (navigateControl) {
      switchPage(navigateControl.dataset.navigate);
      if (navigateControl.dataset.scrollTo) revealAndScroll(navigateControl.dataset.scrollTo);
      return;
    }
    const scrollControl = target.closest("[data-scroll-to]");
    if (scrollControl) {
      revealAndScroll(scrollControl.dataset.scrollTo);
      return;
    }
    if (target.dataset.retryUpload) {
      const item = state.paperUploadQueue.find((candidate) => candidate.id === target.dataset.retryUpload);
      if (!item) throw new Error("没有找到需要重试的文件");
      state.paperUploadRunning = true;
      item.archived = false;
      await processUploadItem(item);
      state.paperUploadRunning = false;
      await loadAll();
      return;
    }
    if (target.dataset.retryParse) {
      const item = state.paperUploadQueue.find((candidate) => candidate.id === target.dataset.uploadItem);
      try {
        if (item) updateUploadItem(item, { status: "parsing", message: "正在恢复单文件解析…" });
        const paper = await api(`/papers/${target.dataset.retryParse}/parse`, { method: "POST" });
        if (paper.status !== "parsed") throw new Error(paper.error_message || "材料解析仍未成功");
        if (item) updateUploadItem(item, { status: "completed", progress: 100, message: "重新解析已完成。" });
        await loadAll();
        showToast("材料已重新解析");
      } catch (error) {
        if (item) updateUploadItem(item, { status: "parse_failed", message: directUploadMessage(error) });
        throw error;
      }
      return;
    }
    if (target.dataset.openBatch) {
      state.selectedBatchId = target.dataset.openBatch;
      state.selectedPaperId = "";
      state.selectedRunId = "";
      await loadAll();
      switchPage("batches");
      return;
    }
    if (target.dataset.aiTest) {
      await api(`/ai-connections/${target.dataset.aiTest}/test`, { method: "POST" });
      showToast("连接已由服务端测试");
      await loadAll();
      return;
    }
    if (target.dataset.memberUpdate) {
      const role = document.querySelector(`[data-member-role="${target.dataset.memberUpdate}"]`)?.value;
      if (!role || !state.currentOrganizationId) throw new Error("无法读取成员角色或当前组织");
      await api(`/organizations/${state.currentOrganizationId}/members/${target.dataset.memberUpdate}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role }),
      });
      await loadAll();
      showToast("成员角色已更新");
      return;
    }
    if (target.dataset.memberReset) {
      if (!state.currentOrganizationId) throw new Error("请选择当前组织");
      const issued = await api(`/organizations/${state.currentOrganizationId}/members/${target.dataset.memberReset}/password-reset-token`, {
        method: "POST",
      });
      state.latestPasswordReset = { memberName: target.dataset.memberName || "该成员", token: issued.reset_token };
      state.latestPasswordReset.url = `${window.location.origin}/reset-password#token=${encodeURIComponent(issued.reset_token)}`;
      renderSettings();
      showToast("重置令牌已生成，请安全转交给成员");
      return;
    }
    if (target.dataset.copyInvitationLink) {
      if (!state.latestInvitationLink) throw new Error("注册链接已失效，请重新创建邀请");
      await navigator.clipboard.writeText(state.latestInvitationLink);
      showToast("注册链接已复制");
      return;
    }
    if (target.dataset.copyResetToken) {
      if (!state.latestPasswordReset?.url) throw new Error("重置链接已失效，请重新生成");
      await navigator.clipboard.writeText(state.latestPasswordReset.url);
      showToast("重置链接已复制");
      return;
    }
    if (target.dataset.aiRotate) {
      const apiKey = window.prompt("输入新的 API Key；它只会发送到本系统后端", "");
      if (!apiKey) return;
      await api(`/ai-connections/${target.dataset.aiRotate}/rotate-key`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: apiKey }),
      });
      showToast("连接密钥已轮换");
      await loadAll();
      return;
    }
    if (target.dataset.aiDisable) {
      await api(`/ai-connections/${target.dataset.aiDisable}/disable`, { method: "POST" });
      showToast("连接已停用；已绑定的旧任务会安全失败");
      await loadAll();
      return;
    }
    if (target.dataset.aiDelete) {
      if (!window.confirm("删除此 AI 连接？已存在的任务不会改用其他连接。")) return;
      await api(`/ai-connections/${target.dataset.aiDelete}`, { method: "DELETE" });
      showToast("连接已删除");
      await loadAll();
      return;
    }
    if (target.dataset.draftRulesAll !== undefined) {
      await draftAIRules(state.editCriteriaDraft);
      return;
    }
    if (target.dataset.draftCriterion) {
      const criterion = state.editCriteriaDraft.find((item) => item.code === target.dataset.draftCriterion);
      if (!criterion) throw new Error("没有找到要起草规则的评分项");
      await draftAIRules([criterion]);
      return;
    }
    if (target.dataset.confirmAiRules) {
      applyConfirmedAIRules(target.dataset.confirmAiRules);
      renderCriteriaBuilder("edit");
      renderRubricEditFeedback();
      showToast("AI 建议已确认并应用，保存后将重新校验");
      return;
    }
    if (target.dataset.confirmAiRulesAll !== undefined) {
      confirmAllAIRules();
      return;
    }
    if (target.dataset.fixBlocker) {
      state.selectedRubricId = state.selectedLifecycleRubricId;
      state.editCriteriaRubricId = "";
      renderRubricEditForm();
      revealAndScroll("template-editor");
      window.setTimeout(() => {
        const card = document.querySelector(`[data-criterion-code="${CSS.escape(target.dataset.fixBlocker)}"]`);
        if (!card) return;
        card.classList.add("field-error");
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        const fieldPath = target.dataset.fixField || "";
        const field = fieldPath.includes("description") ? "description" : fieldPath.includes("scoring_mode") ? "scoring_mode" : "deduction_rules";
        card.querySelector(`[data-criterion-field="${field}"]`)?.focus();
      }, 50);
      return;
    }
    if (target.dataset.addCriterion) {
      addCriterion(target.dataset.addCriterion);
      return;
    }
    if (target.dataset.removeCriterion) {
      const scope = target.dataset.removeCriterion;
      const criteria = builderCriteria(scope);
      if (criteria.length <= 1) throw new Error("模板至少需要一个评分项");
      criteria.splice(Number(target.dataset.index), 1);
      if (scope === "edit") {
        state.rubricEditStatus = "dirty";
        state.rubricEditError = null;
      }
      renderCriteriaBuilder(scope);
      renderRubricEditFeedback();
      return;
    }
    if (target.dataset.action === "reload") {
      await loadAll();
      showToast("数据已刷新");
      return;
    }
    if (batchCard) {
      state.selectedBatchId = batchCard.dataset.selectBatch;
      state.selectedPaperId = "";
      state.selectedRunId = "";
      await loadAll();
      return;
    }
    if (target.dataset.editRubric) {
      state.selectedRubricId = target.dataset.editRubric;
      state.selectedLifecycleRubricId = target.dataset.editRubric;
      state.editCriteriaRubricId = "";
      await refreshRubricLifecycle();
      render();
      revealAndScroll("template-editor");
      return;
    }
    if (target.dataset.openRubricLifecycle) {
      state.selectedLifecycleRubricId = target.dataset.openRubricLifecycle;
      await refreshRubricLifecycle();
      render();
      revealAndScroll("template-review");
      return;
    }
    if (target.dataset.lifecycleAction) {
      await handleRubricLifecycleAction(target);
      return;
    }
    if (target.dataset.cloneRubric) {
      const version = window.prompt("新版本号", "v1.1");
      if (version) {
        await api(`/rubrics/${target.dataset.cloneRubric}/clone`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ new_version: version }),
        });
        showToast("已复制为新版本");
        await loadAll();
      }
    }
    if (target.dataset.saveScore) {
      const itemId = target.dataset.saveScore;
      const score = Number(document.querySelector(`[data-score-input="${itemId}"]`).value);
      const reason = window.prompt("修改原因", "教师复核后调整");
      if (reason) {
        await api(`/score-items/${itemId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ final_score: score, reason }),
        });
        showToast("单项分数已保存");
        await loadAll();
      }
    }
  } catch (error) {
    showToast(error.message, true);
  }
}

async function submitJsonForm(form, path, makePayload, successMessage) {
  const payload = makePayload(new FormData(form));
  const result = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  showToast(successMessage);
  await loadAll();
  return result;
}

function startLlmProcessingStatus() {
  window.clearInterval(state.llmTimer);
  state.llmStatus = { phase: "processing", startedAt: Date.now() };
  renderLlmRuntimeStatus();
  state.llmTimer = window.setInterval(renderLlmRuntimeStatus, 1000);
}

function stopLlmProcessingTimer() {
  window.clearInterval(state.llmTimer);
  state.llmTimer = null;
}

async function summarizeRunFallback(runId) {
  try {
    const items = await api(`/scoring-runs/${runId}/items`);
    return {
      itemCount: items.length,
      fallbackCount: items.filter((item) => (item.deductions || []).some((text) => String(text).includes("降级为 Mock"))).length,
    };
  } catch (_) {
    return { itemCount: 0, fallbackCount: 0 };
  }
}

function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => switchPage(button.dataset.page)));
  document.querySelector("#refresh-btn").addEventListener("click", () => loadAll().catch((error) => showToast(error.message, true)));
  document.body.addEventListener("click", handleAction);
  document.body.addEventListener("input", (event) => {
    const field = event.target.closest?.("[data-criterion-field]");
    if (field) updateCriterionField(field);
    else if (event.target.closest?.("#rubric-edit-form")) {
      state.rubricEditStatus = "dirty";
      state.rubricEditError = null;
      renderRubricEditFeedback();
    }
  });

  for (const [selector, scope] of [["#rubric-form textarea[name='criteria']", "new"], ["#rubric-edit-form textarea[name='criteria']", "edit"]]) {
    document.querySelector(selector).addEventListener("change", (event) => {
      try {
        const parsed = JSON.parse(event.target.value);
        if (!Array.isArray(parsed) || !parsed.length) throw new Error("评分项 JSON 必须是非空数组");
        if (scope === "new") state.newCriteriaDraft = parsed;
        else state.editCriteriaDraft = parsed;
        renderCriteriaBuilder(scope);
        showToast("已同步到可视化编辑器");
      } catch (error) {
        showToast(error.message, true);
      }
    });
  }

  document.querySelector("#rubric-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const created = await submitJsonForm(
        event.currentTarget,
        "/rubrics",
        (form) => {
          const criteria = JSON.parse(form.get("criteria"));
          return {
          name: form.get("name"),
          version: form.get("version"),
          description: form.get("description"),
          visibility: form.get("visibility"),
          total_score: criteria.reduce((sum, item) => sum + Number(item.max_score), 0),
            criteria,
          };
        },
        "评分模板草稿已创建",
      );
      state.selectedRubricId = created.id;
      state.selectedLifecycleRubricId = created.id;
      state.newCriteriaDraft = JSON.parse(JSON.stringify(defaultCriteria));
      render();
      revealAndScroll("template-editor");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#rubric-import-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const formData = new FormData(event.currentTarget);
      if (!formData.get("rules_file")?.size) throw new Error("请上传 Excel 评分规则");
      if (!formData.get("template_file")?.size) formData.delete("template_file");
      const imported = await api("/rubrics/import-files", { method: "POST", body: formData });
      state.rubricImportPreview = imported;
      state.selectedLifecycleRubricId = imported.rubric.id;
      state.selectedRubricId = imported.rubric.id;
      state.editCriteriaRubricId = "";
      showToast("已从文件生成草稿评分标准");
      await loadAll();
      revealAndScroll("template-editor");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#download-rubric-template-btn").addEventListener("click", () => {
    window.open(`${apiBase()}/rubrics/import-template.xlsx`, "_blank");
  });

  document.querySelector("#rubric-edit-select").addEventListener("change", async (event) => {
    state.selectedRubricId = event.target.value;
    state.selectedLifecycleRubricId = event.target.value;
    state.editCriteriaRubricId = "";
    await refreshRubricLifecycle();
    render();
  });

  document.querySelector("#rubric-lifecycle-select").addEventListener("change", async (event) => {
    state.selectedLifecycleRubricId = event.target.value;
    await refreshRubricLifecycle();
    render();
  });

  document.querySelector("#rubric-edit-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      if (!state.selectedRubricId) throw new Error("请选择草稿评分标准");
      if (Object.keys(state.aiRuleDrafts).length) {
        const error = new Error("AI 已生成扣分细则，但尚未确认。");
        error.problem = {
          code: "AI_DRAFT_PENDING_CONFIRMATION",
          message: "AI 已生成扣分细则，但尚未确认。",
          user_action: "请确认并应用 AI 建议，或排除不需要的规则后再保存。",
          retryable: false,
        };
        throw error;
      }
      const form = new FormData(event.currentTarget);
      const criteria = JSON.parse(form.get("criteria"));
      if (state.selectedLifecycleRubricId !== state.selectedRubricId) {
        state.selectedLifecycleRubricId = state.selectedRubricId;
        await refreshRubricLifecycle();
      }
      const active = state.rubricLifecycle?.active_compilation;
      if (!active?.id) throw new Error("当前没有可重新校验的执行草稿，请刷新后重试");
      state.rubricEditStatus = "saving";
      state.rubricEditError = null;
      renderRubricEditFeedback();
      await api(`/rubrics/${state.selectedRubricId}/recompile`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          supersedes_compilation_id: active.id,
          reason: "用户在模板中心确认修改并重新校验",
          name: String(form.get("name") || "").trim(),
          version: String(form.get("version") || "").trim(),
          description: optionalText(form.get("description")),
          total_score: criteria.reduce((sum, item) => sum + Number(item.max_score), 0),
          criteria,
        }),
      });
      state.editCriteriaRubricId = "";
      state.rubricEditStatus = "clean";
      state.rubricEditError = null;
      showToast("模板已保存并生成新的执行草稿");
      await loadAll();
      revealAndScroll("template-review");
    } catch (error) {
      renderRubricOperationError(error);
    }
  });

  document.querySelector("#batch-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const created = await submitJsonForm(
        event.currentTarget,
        "/batches",
        (form) => ({
          name: form.get("name"),
          rubric_id: form.get("rubric_id"),
          ai_connection_id: optionalText(form.get("ai_connection_id")),
          department: form.get("department"),
          major: form.get("major"),
        }),
        "评分任务已创建",
      );
      state.selectedBatchId = created.id;
      await loadAll();
      revealAndScroll("paper-files");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#rubric-visibility-filter")?.addEventListener("change", (event) => {
    state.rubricVisibilityFilter = event.target.value;
    renderRubrics();
  });

  document.querySelector("#organization-switcher")?.addEventListener("change", async (event) => {
    try {
      const selected = event.target.value;
      await api("/auth/organization-context", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ organization_id: selected }),
      });
      state.identity = await api("/auth/me");
      state.currentOrganizationId = state.identity.organization?.id || "";
      state.latestInvitationLink = "";
      state.latestPasswordReset = null;
      state.selectedBatchId = "";
      state.selectedPaperId = "";
      state.selectedRunId = "";
      await loadAll();
      showToast("已切换当前组织");
    } catch (error) {
      showToast(error.message, true);
      renderSettings();
    }
  });

  document.querySelector("#organization-invite-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const inviteForm = event.currentTarget;
    try {
      if (!state.currentOrganizationId) throw new Error("请选择当前组织");
      const form = new FormData(inviteForm);
      const invitation = await api(`/organizations/${state.currentOrganizationId}/members`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: String(form.get("email") || "").trim(), role: form.get("role") }),
      });
      if (!invitation.invitation_token) throw new Error("服务未返回邀请令牌，请重新创建邀请");
      // The fragment is never sent in HTTP requests, avoiding disclosure in
      // server logs while still allowing the registration form to receive it.
      state.latestInvitationLink = `${window.location.origin}/register#invite=${encodeURIComponent(invitation.invitation_token)}`;
      inviteForm.reset();
      renderSettings();
      showToast("邀请已创建，请复制并安全转发注册链接");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#ai-connection-test-draft")?.addEventListener("click", async () => {
    try {
      const form = new FormData(document.querySelector("#ai-connection-form"));
      await api("/ai-connections/test-draft", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(aiConnectionPayload(form)),
      });
      showToast("配置已由服务端测试，尚未保存");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#ai-connection-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const connectionForm = event.currentTarget;
    try {
      const form = new FormData(connectionForm);
      await api("/ai-connections", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(aiConnectionPayload(form)),
      });
      connectionForm.elements.api_key.value = "";
      await loadAll();
      showToast("私有 AI 连接已加密保存");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#paper-edit-select").addEventListener("change", (event) => {
    state.selectedPaperId = event.target.value;
    render();
  });

  document.querySelector("#paper-edit-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      if (!state.selectedPaperId) throw new Error("请选择论文");
      const form = new FormData(event.currentTarget);
      await api(`/papers/${state.selectedPaperId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: optionalText(form.get("title")),
          student_id: optionalText(form.get("student_id")),
          student_name: optionalText(form.get("student_name")),
          department: optionalText(form.get("department")),
          major: optionalText(form.get("major")),
          advisor: optionalText(form.get("advisor")),
        }),
      });
      showToast("论文信息已保存");
      await loadAll();
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#upload-btn").addEventListener("click", async () => {
    try {
      if (!state.selectedBatchId) throw new Error("请先选择或创建评分任务");
      const files = document.querySelector("#paper-files").files;
      if (!files.length) throw new Error("请选择待评材料");
      await runPaperUploads(files);
      document.querySelector("#paper-files").value = "";
      revealAndScroll("batch-score-job-panel");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#score-batch-btn").addEventListener("click", async () => {
    try {
      if (!state.selectedBatchId) throw new Error("请先选择评分任务");
      if (!state.papers.length) throw new Error("请先上传待评材料");
      const pending = state.papers.filter((paper) => paper.status !== "parsed");
      if (pending.length) throw new Error(`请先处理 ${pending.length} 份尚未完成解析的材料`);
      const config = document.querySelector(".run-config");
      if (config) config.open = true;
      revealAndScroll("batch-score-job-form");
      showToast(state.batchScoreJob ? "运行条件已加载，可继续运行或恢复任务" : "请填写经批准的观察策略后创建评分任务");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#batch-score-job-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      if (!state.selectedBatchId) throw new Error("请选择批次");
      const pending = state.papers.filter((paper) => paper.status !== "parsed");
      if (pending.length) throw new Error(`请先处理 ${pending.length} 份尚未完成解析的材料`);
      const form = new FormData(event.currentTarget);
      const policyText = String(form.get("observation_policy") || "").trim();
      if (!policyText) throw new Error("请粘贴经批准的观察策略 JSON");
      const observationPolicy = JSON.parse(policyText);
      state.batchScoreJob = await api(`/batches/${state.selectedBatchId}/score-jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          rescore: form.get("rescore") === "on",
          max_workers: Number(form.get("max_workers")),
          observation_policy: observationPolicy,
        }),
      });
      renderBatchScoreJob();
      showToast("持久化批量评分任务已创建");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#run-batch-score-job-btn").addEventListener("click", async () => {
    try {
      if (!state.batchScoreJob) throw new Error("请先创建任务");
      state.batchScoreJob = await api(`/batch-scoring-jobs/${state.batchScoreJob.id}/run`, { method: "POST" });
      renderBatchScoreJob();
      await loadAll();
      showToast("批量评分任务已完成并持久化检查点");
    } catch (error) {
      await refreshBatchScoreJob();
      renderBatchScoreJob();
      showToast(error.message, true);
    }
  });

  document.querySelector("#cancel-batch-score-job-btn").addEventListener("click", async () => {
    try {
      if (!state.batchScoreJob) throw new Error("没有可取消的任务");
      state.batchScoreJob = await api(`/batch-scoring-jobs/${state.batchScoreJob.id}/cancel`, { method: "POST" });
      renderBatchScoreJob();
      showToast("取消状态已持久化");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#retry-batch-score-job-btn").addEventListener("click", async () => {
    try {
      if (!state.batchScoreJob) throw new Error("没有可重试的任务");
      state.batchScoreJob = await api(`/batch-scoring-jobs/${state.batchScoreJob.id}/retry`, { method: "POST" });
      renderBatchScoreJob();
      showToast("失败/取消项已恢复为待执行");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#score-paper-btn").addEventListener("click", async () => {
    try {
      if (!state.selectedPaperId) throw new Error("请选择论文");
      startLlmProcessingStatus();
      const run = await api(`/papers/${state.selectedPaperId}/score`, { method: "POST" });
      const fallbackSummary = await summarizeRunFallback(run.id);
      state.selectedRunId = run.id;
      await loadAll();
      stopLlmProcessingTimer();
      state.llmStatus = { phase: "done", run, ...fallbackSummary };
      renderLlmRuntimeStatus();
      showToast(fallbackSummary.fallbackCount ? "评分完成，真实 LLM 已降级并标记复核" : "评分完成");
    } catch (error) {
      stopLlmProcessingTimer();
      state.llmStatus = { phase: "error", message: error.message };
      renderLlmRuntimeStatus();
      showToast(error.message, true);
    }
  });

  document.querySelector("#submit-review-btn")?.addEventListener("click", () => {});

  document.querySelector("#review-batch-select").addEventListener("change", async (event) => {
    state.selectedBatchId = event.target.value;
    state.selectedPaperId = "";
    state.selectedRunId = "";
    await loadAll();
  });
  document.querySelector("#review-paper-select").addEventListener("change", async (event) => {
    state.selectedPaperId = event.target.value;
    state.runs = state.selectedPaperId ? await api(`/scoring-runs?paper_id=${state.selectedPaperId}`) : [];
    state.selectedRunId = state.runs[0]?.id || "";
    render();
  });
  document.querySelector("#run-select").addEventListener("change", (event) => {
    state.selectedRunId = event.target.value;
    render();
  });
  document.querySelector("#export-batch-select").addEventListener("change", async (event) => {
    state.selectedBatchId = event.target.value;
    state.runs = await api(`/scoring-runs?batch_id=${state.selectedBatchId}`);
    state.selectedRunId = state.runs[0]?.id || "";
    await refreshExportLogs();
    render();
  });
  document.querySelector("#export-run-select").addEventListener("change", (event) => {
    state.selectedRunId = event.target.value;
    render();
  });

  document.querySelector("#export-excel-btn").addEventListener("click", () => {
    if (!state.selectedBatchId) return showToast("请选择批次", true);
    window.open(`${apiBase()}/batches/${state.selectedBatchId}/export.xlsx`, "_blank");
  });
  document.querySelector("#write-sheet-btn").addEventListener("click", async () => {
    try {
      if (!state.selectedRunId) throw new Error("请选择评分任务");
      const targetId = document.querySelector("#sheet-target").value;
      const log = await api(`/scoring-runs/${state.selectedRunId}/write-sheet`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_id: targetId }),
      });
      document.querySelector("#export-result").innerHTML = `<pre class="context">${escapeHtml(JSON.stringify(log.response, null, 2))}</pre>`;
      await refreshExportLogs();
      renderExports();
      showToast("写表已完成");
    } catch (error) {
      showToast(error.message, true);
    }
  });
  document.querySelector("#refresh-export-logs-btn").addEventListener("click", async () => {
    try {
      await refreshExportLogs();
      renderExports();
      showToast("写表记录已刷新");
    } catch (error) {
      showToast(error.message, true);
    }
  });
  document.querySelector("#report-btn").addEventListener("click", () => {
    if (!state.selectedRunId) return showToast("请选择评分任务", true);
    window.open(`${apiBase()}/scoring-runs/${state.selectedRunId}/report`, "_blank");
  });

  document.querySelector("#llm-check-btn")?.addEventListener("click", async () => {
    const el = document.querySelector("#llm-check-result");
    el.innerHTML = '<div class="muted">连通测试中…</div>';
    try {
      const result = await api("/system/llm-check");
      el.innerHTML = `<pre class="context">${escapeHtml(JSON.stringify(result, null, 2))}</pre>`;
      showToast(
        result.ok ? `连通正常（${result.stage}${result.latency_ms ? `, ${result.latency_ms}ms` : ""}）` : `未通过：${result.error || result.stage}`,
        !result.ok,
      );
    } catch (error) {
      el.innerHTML = `<div class="muted">${escapeHtml(error.message)}</div>`;
      showToast(error.message, true);
    }
  });

  document.querySelector("#anchor-rubric-select")?.addEventListener("change", async (event) => {
    state.anchorRubricId = event.target.value;
    try {
      await refreshAnchors();
      renderCalibration();
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#anchor-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const anchorForm = event.currentTarget;
    try {
      if (!state.anchorRubricId) throw new Error("请选择评分标准");
      const form = new FormData(anchorForm);
      await api("/calibration/anchors", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          rubric_id: state.anchorRubricId,
          criterion_code: String(form.get("criterion_code") || "").trim(),
          score: Number(form.get("score")),
          max_score: Number(form.get("max_score")),
          label: optionalText(form.get("label")),
          excerpt: String(form.get("excerpt") || "").trim(),
          rationale: optionalText(form.get("rationale")),
        }),
      });
      anchorForm.reset();
      await refreshAnchors();
      renderCalibration();
      showToast("锚点已添加");
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.body.addEventListener("click", async (event) => {
    if (event.target.id === "submit-review-btn") {
      try {
        if (!state.selectedRunId) throw new Error("请选择评分任务");
        const reason = document.querySelector("#review-reason").value.trim();
        if (!reason) throw new Error("请填写复核意见");
        await api(`/scoring-runs/${state.selectedRunId}/review`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason }),
        });
        showToast("复核已提交");
        await loadAll();
      } catch (error) {
        showToast(error.message, true);
      }
    }
  });
}

function bindAuth() {
  const accountTrigger = document.querySelector("#account-menu-trigger");
  if (accountTrigger) accountTrigger.addEventListener("click", toggleAccountMenu);
  document.addEventListener("click", (event) => {
    if (!event.target.closest?.("#sidebar-account")) closeAccountMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeAccountMenu();
  });

  const form = document.querySelector("#login-form");
  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const username = document.querySelector("#login-username").value.trim();
      const password = document.querySelector("#login-password").value;
      try {
        const res = await fetch(`${apiBase()}/auth/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ username, password }),
        });
        setConnectionStatus("healthy");
        if (!res.ok) {
          showLogin("用户名或密码错误");
          return;
        }
        hideLogin();
        await refreshAuthState();
        await loadAll();
      } catch (error) {
        setConnectionStatus("failed");
        showLogin(error.message);
      }
    });
  }
  const logout = document.querySelector("#logout-btn");
  if (logout) {
    logout.addEventListener("click", async () => {
      closeAccountMenu();
      try {
        await fetch(`${apiBase()}/auth/logout`, { method: "POST", credentials: "same-origin" });
      } catch (_) {
        // The local UI must still leave the authenticated state after a network failure.
      }
      state.identity = null;
      state.organizations = [];
      state.currentOrganizationId = "";
      state.latestInvitationLink = "";
      state.latestPasswordReset = null;
      setLogoutVisible(false);
      setSidebarUser(null);
      showLogin("已登出");
    });
  }

  const registerForm = document.querySelector("#register-form");
  if (registerForm) {
    registerForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      setAuthError("#register-error", "");
      try {
        const form = new FormData(registerForm);
        const payload = Object.fromEntries(form.entries());
        if (payload.password !== payload.password_confirmation) {
          setAuthError("#register-error", "两次输入的密码不一致");
          return;
        }
        delete payload.password_confirmation;
        await api("/auth/register", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        showLogin("注册成功，请使用用户名和密码登录。");
      } catch (error) {
        setAuthError("#register-error", error.message);
      }
    });
  }
  const resetConfirmForm = document.querySelector("#password-reset-confirm-form");
  if (resetConfirmForm) {
    resetConfirmForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      setAuthError("#reset-error", "");
      try {
        const form = new FormData(resetConfirmForm);
        const password = String(form.get("password") || "");
        const confirmation = String(form.get("password_confirmation") || "");
        if (password !== confirmation) {
          setAuthError("#reset-error", "两次输入的密码不一致");
          return;
        }
        await api("/auth/password-reset/confirm", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: String(form.get("token") || "").trim(), password, password_confirmation: confirmation }),
        });
        showLogin("密码已更新，请使用新密码登录。");
      } catch (error) {
        setAuthError("#reset-error", error.message);
      }
    });
  }

  prepareAuthPageFromLocation();
}

async function prepareRegistrationPage() {
  const form = document.querySelector("#register-form");
  const pending = document.querySelector("#register-pending");
  const invalid = document.querySelector("#register-invalid");
  if (!form || !pending || !invalid) return;
  form.classList.add("hidden");
  invalid.classList.add("hidden");
  pending.classList.remove("hidden");
  const invitationToken = takeFragmentToken("invite");
  if (!invitationToken) {
    pending.classList.add("hidden");
    invalid.classList.remove("hidden");
    return;
  }
  try {
    const invitation = await api("/auth/invitations/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: invitationToken }),
    });
    form.elements.namedItem("email").value = invitation.email;
    form.elements.namedItem("invitation_token").value = invitationToken;
    document.querySelector("#register-organization-name").textContent = invitation.organization_name;
    document.querySelector("#register-invited-email").textContent = invitation.email;
    document.querySelector("#register-role-hint").textContent = `加入后角色：${invitation.role}`;
    pending.classList.add("hidden");
    form.classList.remove("hidden");
  } catch (_) {
    pending.classList.add("hidden");
    invalid.classList.remove("hidden");
  }
}

function prepareResetPasswordPage() {
  const form = document.querySelector("#password-reset-confirm-form");
  if (!form) return;
  const token = takeFragmentToken("token");
  if (!token) return;
  form.elements.namedItem("token").value = token;
  document.querySelector("#reset-token-field")?.classList.add("hidden");
  document.querySelector("#reset-token-notice")?.classList.remove("hidden");
}

function prepareAuthPageFromLocation() {
  const page = currentAuthPage();
  if (!page) return;
  showAuthPage(page, { updateUrl: false });
  if (page === "register") prepareRegistrationPage();
  if (page === "reset") prepareResetPasswordPage();
}

bindEvents();
bindAuth();
(async () => {
  const ready = await refreshAuthState();
  if (ready) loadAll().catch((error) => showToast(error.message, true));
})();
