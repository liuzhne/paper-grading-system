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
];

const state = {
  page: "dashboard",
  rubrics: [],
  batches: [],
  papers: [],
  runs: [],
  integrations: null,
  selectedRubricId: "",
  selectedBatchId: "",
  selectedPaperId: "",
  selectedRunId: "",
  chunksById: {},
  exportLogs: [],
  ranking: null,
  drift: null,
  anchors: [],
  anchorRubricId: "",
  llmStatus: { phase: "idle" },
  llmTimer: null,
};

const pageMeta = {
  dashboard: ["总览", "查看评分批次、论文状态和待复核工作量。"],
  rubrics: ["评分标准", "维护标准版本，也可以从 Word 模板和 Excel 规则导入。"],
  batches: ["批次与论文", "创建批次、上传论文、解析并发起批量评分。"],
  review: ["评分复核", "查看评分依据、上下文，并提交人工复核。"],
  exports: ["导出写表", "下载 Excel 报表、生成 HTML 报告或写入在线表格。"],
};

function apiBase() {
  return document.querySelector("#api-base").value.replace(/\/$/, "");
}

function authToken() {
  return window.localStorage.getItem("pgs_token") || "";
}
function setAuthToken(token) {
  if (token) window.localStorage.setItem("pgs_token", token);
  else window.localStorage.removeItem("pgs_token");
}

function showLogin(message) {
  const overlay = document.querySelector("#login-overlay");
  if (!overlay) return;
  const err = document.querySelector("#login-error");
  if (err) {
    if (message) {
      err.textContent = message;
      err.classList.remove("hidden");
    } else {
      err.classList.add("hidden");
    }
  }
  overlay.classList.remove("hidden");
  const pwd = document.querySelector("#login-password");
  if (pwd) pwd.value = "";
}

function hideLogin() {
  const overlay = document.querySelector("#login-overlay");
  if (overlay) overlay.classList.add("hidden");
}

function setLogoutVisible(visible) {
  const btn = document.querySelector("#logout-btn");
  if (btn) btn.classList.toggle("hidden", !visible);
}

async function refreshAuthState() {
  // 返回 true=可进入应用；false=需登录（已弹出登录框）。
  try {
    const status = await (await fetch(`${apiBase()}/auth/status`)).json();
    if (!status.auth_required) {
      setLogoutVisible(false);
      return true;
    }
    const token = authToken();
    if (token) {
      const me = await fetch(`${apiBase()}/auth/me`, { headers: { Authorization: `Bearer ${token}` } });
      if (me.ok) {
        setLogoutVisible(true);
        return true;
      }
      setAuthToken("");
    }
    showLogin();
    return false;
  } catch (_) {
    return true; // 自检失败不阻塞（如离线/旧后端）
  }
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  const token = authToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const response = await fetch(`${apiBase()}${path}`, Object.assign({}, options, { headers }));
  if (response.status === 401) {
    setAuthToken("");
    setLogoutVisible(false);
    showLogin("登录已失效，请重新登录");
    throw new Error("需要登录后重试");
  }
  if (!response.ok) {
    let detail = response.statusText;
    const text = await response.text();
    try {
      const payload = text ? JSON.parse(text) : {};
      detail = payload.detail || JSON.stringify(payload);
    } catch (_) {
      detail = text || response.statusText;
    }
    throw new Error(detail);
  }
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) return response.json();
  return response;
}

function showToast(message, isError = false) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.className = `toast${isError ? " error" : ""}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 3600);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
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
  state.integrations = await api("/system/integrations");
  state.rubrics = await api("/rubrics");
  state.batches = await api("/batches");
  if (!state.selectedBatchId && state.batches[0]) state.selectedBatchId = state.batches[0].id;
  state.papers = state.selectedBatchId ? await api(`/papers?batch_id=${state.selectedBatchId}`) : [];
  if (!state.papers.some((paper) => paper.id === state.selectedPaperId)) {
    state.selectedPaperId = state.papers[0]?.id || "";
  }
  state.runs = state.selectedBatchId ? await api(`/scoring-runs?batch_id=${state.selectedBatchId}`) : [];
  if (!state.runs.some((run) => run.id === state.selectedRunId)) {
    state.selectedRunId = state.runs[0]?.id || "";
  }
  state.ranking = state.selectedBatchId ? await api(`/batches/${state.selectedBatchId}/ranking`) : null;
  state.drift = state.selectedBatchId ? await api(`/batches/${state.selectedBatchId}/drift`) : null;
  if (!state.rubrics.some((rubric) => rubric.id === state.anchorRubricId)) {
    state.anchorRubricId = state.rubrics[0]?.id || "";
  }
  await refreshAnchors();
  await refreshExportLogs();
  render();
}

async function refreshAnchors() {
  state.anchors = state.anchorRubricId ? await api(`/calibration/anchors?rubric_id=${state.anchorRubricId}`) : [];
}

function switchPage(page) {
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
  renderCalibration();
  renderBatches();
  renderReview();
  renderExports();
}

function renderSharedSelects() {
  const rubricSelect = document.querySelector('#batch-form select[name="rubric_id"]');
  rubricSelect.innerHTML = optionHtml(state.rubrics, "id", (item) => `${item.name} / ${item.version} / ${item.status}`);

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
  renderIntegrationStatus();
  document.querySelector("#dashboard-batches").innerHTML = renderTable(
    [
      { label: "批次", value: (row) => row.name },
      { label: "学院", value: (row) => row.department || "" },
      { label: "专业", value: (row) => row.major || "" },
      { label: "状态", value: (row) => row.status },
      { label: "创建时间", value: (row) => (row.created_at || "").slice(0, 19) },
    ],
    state.batches,
  );
  renderBatchAnalytics();
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
  if (["local", "llama", "llamacpp", "llama_cpp", "vllm", "ollama"].includes(llm.provider)) return "本地私有模型";
  if (["openai_compatible", "zhipu", "bigmodel"].includes(llm.provider)) return "国内兼容模型";
  if (["qwen", "dashscope"].includes(llm.provider)) return "阿里云百炼";
  if (llm.provider === "openai") return "OpenAI";
  return llm.provider || "LLM";
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
  document.querySelector('#rubric-form textarea[name="criteria"]').value ||= JSON.stringify(defaultCriteria, null, 2);
  document.querySelector("#rubric-list").innerHTML =
    state.rubrics
      .map(
        (rubric) => `<article class="item-card">
          <div class="item-title"><span>${escapeHtml(rubric.name)}</span><span class="badge">${escapeHtml(rubric.status)}</span></div>
          <div class="muted">${escapeHtml(rubric.version)} / ${rubric.total_score} 分 / ${rubric.criteria.length} 项</div>
          <div>${escapeHtml(rubric.description || "")}</div>
          <div class="toolbar">
            <button class="secondary" data-publish-rubric="${rubric.id}">发布</button>
            <button class="secondary" data-clone-rubric="${rubric.id}">复制新版本</button>
          </div>
        </article>`,
      )
      .join("") || '<div class="muted">暂无评分标准</div>';
  renderRubricEditForm();
}

function renderRubricEditForm() {
  const form = document.querySelector("#rubric-edit-form");
  const select = document.querySelector("#rubric-edit-select");
  const draftRubrics = state.rubrics.filter((rubric) => rubric.status === "draft");
  if (!draftRubrics.some((rubric) => rubric.id === state.selectedRubricId)) {
    state.selectedRubricId = draftRubrics[0]?.id || "";
  }
  const rubric = draftRubrics.find((item) => item.id === state.selectedRubricId);
  select.innerHTML = optionHtml(draftRubrics, "id", (item) => `${item.name} / ${item.version}`, state.selectedRubricId);
  select.disabled = !draftRubrics.length;

  form.elements.name.value = rubric?.name || "";
  form.elements.version.value = rubric?.version || "";
  form.elements.description.value = rubric?.description || "";
  form.elements.criteria.value = rubric ? JSON.stringify(criteriaPayloadRows(rubric.criteria || []), null, 2) : "";
  for (const field of ["name", "version", "description", "criteria"]) {
    form.elements[field].disabled = !rubric;
  }
  form.querySelector('button[type="submit"]').disabled = !rubric;
}

function renderBatches() {
  document.querySelector("#batch-list").innerHTML =
    state.batches
      .map(
        (batch) => `<article class="item-card ${batch.id === state.selectedBatchId ? "selected" : ""}" data-select-batch="${batch.id}">
          <div class="item-title"><span>${escapeHtml(batch.name)}</span><span class="badge">${escapeHtml(batch.status)}</span></div>
          <div class="muted">${escapeHtml(batch.department || "")} ${escapeHtml(batch.major || "")}</div>
        </article>`,
      )
      .join("") || '<div class="muted">暂无批次</div>';

  document.querySelector("#paper-list").innerHTML = renderTable(
    [
      { label: "学生", value: (row) => row.student_name || row.student_id || "未知" },
      { label: "论文", value: (row) => row.title || row.file_name },
      { label: "状态", value: (row) => row.status },
      { label: "解析质量", value: (row) => row.parse_quality ?? "" },
    ],
    state.papers,
  );
  renderPaperEditForm();
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
        <label>复核意见<textarea id="review-reason" placeholder="填写整体复核意见"></textarea></label>
        <button id="submit-review-btn">提交复核</button>
      </div>`
    : '<div class="muted">暂无评分任务</div>';
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
    button.textContent = isProcessing ? "评分中..." : "评分选中论文";
  }

  if (status.phase === "processing") {
    const elapsedSeconds = Math.max(0, Math.floor((Date.now() - status.startedAt) / 1000));
    container.innerHTML = llmStatusHtml({
      tone: "active",
      badge: "LLM 处理中",
      title: "正在评分选中论文",
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
  return `${llmDetail(llm)}。按证据块逐块评分并由后端汇总，普通情况最高按80%控制。${thinking}${jsonMode}${fallback}`;
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
  return `${paper.student_name || paper.student_id || "未知"} / ${paper.title || paper.file_name} / ${paper.status}`;
}

function runLabel(run) {
  return `${(run.created_at || "").slice(0, 19)} / ${run.status} / ${run.final_total_score ?? ""} / ${run.grade || ""}`;
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

async function handleAction(event) {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const batchCard = target.closest("[data-select-batch]");
  try {
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
    if (target.dataset.publishRubric) {
      await api(`/rubrics/${target.dataset.publishRubric}/publish`, { method: "POST" });
      showToast("评分标准已发布");
      await loadAll();
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
  await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  showToast(successMessage);
  await loadAll();
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

  document.querySelector("#rubric-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await submitJsonForm(
        event.currentTarget,
        "/rubrics",
        (form) => {
          const criteria = JSON.parse(form.get("criteria"));
          return {
            name: form.get("name"),
            version: form.get("version"),
            description: form.get("description"),
            total_score: criteria.reduce((sum, item) => sum + Number(item.max_score), 0),
            criteria,
          };
        },
        "评分标准已创建",
      );
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
      await api("/rubrics/import-files", { method: "POST", body: formData });
      showToast("已从文件生成草稿评分标准");
      await loadAll();
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#download-rubric-template-btn").addEventListener("click", () => {
    window.open(`${apiBase()}/rubrics/import-template.xlsx`, "_blank");
  });

  document.querySelector("#rubric-edit-select").addEventListener("change", (event) => {
    state.selectedRubricId = event.target.value;
    render();
  });

  document.querySelector("#rubric-edit-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      if (!state.selectedRubricId) throw new Error("请选择草稿评分标准");
      const form = new FormData(event.currentTarget);
      const criteria = JSON.parse(form.get("criteria"));
      await api(`/rubrics/${state.selectedRubricId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: String(form.get("name") || "").trim(),
          version: String(form.get("version") || "").trim(),
          description: optionalText(form.get("description")),
          total_score: criteria.reduce((sum, item) => sum + Number(item.max_score), 0),
          criteria,
        }),
      });
      showToast("草稿评分标准已保存");
      await loadAll();
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#batch-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await submitJsonForm(
        event.currentTarget,
        "/batches",
        (form) => ({
          name: form.get("name"),
          rubric_id: form.get("rubric_id"),
          department: form.get("department"),
          major: form.get("major"),
        }),
        "批次已创建",
      );
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
      if (!state.selectedBatchId) throw new Error("请选择批次");
      const files = document.querySelector("#paper-files").files;
      if (!files.length) throw new Error("请选择论文文件");
      const formData = new FormData();
      formData.append("batch_id", state.selectedBatchId);
      for (const file of files) formData.append("files", file);
      await api("/papers/bulk-upload", { method: "POST", body: formData });
      showToast("论文已上传并解析");
      await loadAll();
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.querySelector("#score-batch-btn").addEventListener("click", async () => {
    try {
      if (!state.selectedBatchId) throw new Error("请选择批次");
      await api(`/batches/${state.selectedBatchId}/score`, { method: "POST" });
      showToast("批量评分完成");
      await loadAll();
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
    try {
      if (!state.anchorRubricId) throw new Error("请选择评分标准");
      const form = new FormData(event.currentTarget);
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
      event.currentTarget.reset();
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
          body: JSON.stringify({ username, password }),
        });
        if (!res.ok) {
          showLogin("用户名或密码错误");
          return;
        }
        setAuthToken((await res.json()).token);
        hideLogin();
        setLogoutVisible(true);
        await loadAll();
      } catch (error) {
        showLogin(error.message);
      }
    });
  }
  const logout = document.querySelector("#logout-btn");
  if (logout) {
    logout.addEventListener("click", () => {
      setAuthToken("");
      setLogoutVisible(false);
      showLogin("已登出");
    });
  }
}

bindEvents();
bindAuth();
(async () => {
  const ready = await refreshAuthState();
  if (ready) loadAll().catch((error) => showToast(error.message, true));
})();
